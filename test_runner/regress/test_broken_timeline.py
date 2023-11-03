import concurrent.futures
import os
from typing import List, Tuple

import pytest
from fixtures.log_helper import log
from fixtures.neon_fixtures import (
    Endpoint,
    NeonEnv,
    NeonEnvBuilder,
    wait_for_last_flush_lsn,
)
from fixtures.types import TenantId, TimelineId


# Test restarting page server, while safekeeper and compute node keep
# running.
def test_local_corruption(neon_env_builder: NeonEnvBuilder):
    env = neon_env_builder.init_start()

    env.pageserver.allowed_errors.extend(
        [
            ".*layer loading failed:.*",
            ".*could not find data for key.*",
            ".*is not active. Current state: Broken.*",
            ".*will not become active. Current state: Broken.*",
            ".*failed to load metadata.*",
            ".*load failed.*load local timeline.*",
        ]
    )

    tenant_timelines: List[Tuple[TenantId, TimelineId, Endpoint]] = []

    for _ in range(3):
        tenant_id, timeline_id = env.neon_cli.create_tenant()

        endpoint = env.endpoints.create_start("main", tenant_id=tenant_id)
        with endpoint.cursor() as cur:
            cur.execute("CREATE TABLE t(key int primary key, value text)")
            cur.execute("INSERT INTO t SELECT generate_series(1,100), 'payload'")
            wait_for_last_flush_lsn(env, endpoint, tenant_id, timeline_id)
        endpoint.stop()
        tenant_timelines.append((tenant_id, timeline_id, endpoint))

    # Stop the pageserver -- this has to be not immediate or we need to wait for uploads
    env.pageserver.stop()

    # Leave the first timeline alone, but corrupt the others in different ways
    (tenant0, timeline0, pg0) = tenant_timelines[0]
    log.info(f"Timeline {tenant0}/{timeline0} is left intact")

    (tenant1, timeline1, pg1) = tenant_timelines[1]
    metadata_path = f"{env.pageserver.workdir}/tenants/{tenant1}/timelines/{timeline1}/metadata"
    with open(metadata_path, "w") as f:
        f.write("overwritten with garbage!")
    log.info(f"Timeline {tenant1}/{timeline1} got its metadata spoiled")

    (tenant2, timeline2, pg2) = tenant_timelines[2]
    timeline_path = f"{env.pageserver.workdir}/tenants/{tenant2}/timelines/{timeline2}/"
    for filename in os.listdir(timeline_path):
        if filename.startswith("00000"):
            # Looks like a layer file. Corrupt it
            p = f"{timeline_path}/{filename}"
            size = os.path.getsize(p)
            with open(p, "wb") as f:
                f.truncate(0)
                f.truncate(size)
    log.info(f"Timeline {tenant2}/{timeline2} got its local layer files spoiled")

    env.pageserver.start()

    # Un-damaged tenant works
    pg0.start()
    assert pg0.safe_psql("SELECT COUNT(*) FROM t")[0][0] == 100

    # Tenant with corrupt local metadata works: remote storage is authoritative for metadata
    pg1.start()
    assert pg1.safe_psql("SELECT COUNT(*) FROM t")[0][0] == 100

    # Second timeline will fail during basebackup, because the local layer file is corrupt.
    # It will fail when we try to read (and reconstruct) a page from it, ergo the error message.
    # (We don't check layer file contents on startup, when loading the timeline)
    #
    # This will change when we implement checksums for layers
    with pytest.raises(Exception, match="layer loading failed:") as err:
        pg2.start()
    log.info(
        f"As expected, compute startup failed for timeline {tenant2}/{timeline2} with corrupt layers: {err}"
    )


def test_create_multiple_timelines_parallel(neon_simple_env: NeonEnv):
    env = neon_simple_env

    tenant_id, _ = env.neon_cli.create_tenant()

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(
                env.neon_cli.create_timeline, f"test-create-multiple-timelines-{i}", tenant_id
            )
            for i in range(4)
        ]
        for future in futures:
            future.result()


def test_timeline_init_break_before_checkpoint(neon_env_builder: NeonEnvBuilder):
    env = neon_env_builder.init_start()
    pageserver_http = env.pageserver.http_client()

    env.pageserver.allowed_errors.extend(
        [
            ".*Failed to process timeline dir contents.*Timeline has no ancestor and no layer files.*",
            ".*Timeline got dropped without initializing, cleaning its files.*",
        ]
    )

    tenant_id = env.initial_tenant

    timelines_dir = env.pageserver.timeline_dir(tenant_id)
    old_tenant_timelines = env.neon_cli.list_timelines(tenant_id)
    initial_timeline_dirs = list(timelines_dir.iterdir())

    # Introduce failpoint during timeline init (some intermediate files are on disk), before it's checkpointed.
    pageserver_http.configure_failpoints(("before-checkpoint-new-timeline", "return"))
    with pytest.raises(Exception, match="before-checkpoint-new-timeline"):
        _ = env.neon_cli.create_timeline("test_timeline_init_break_before_checkpoint", tenant_id)

    # Restart the page server
    env.pageserver.restart(immediate=True)

    # Creating the timeline didn't finish. The other timelines on tenant should still be present and work normally.
    new_tenant_timelines = env.neon_cli.list_timelines(tenant_id)
    assert (
        new_tenant_timelines == old_tenant_timelines
    ), f"Pageserver after restart should ignore non-initialized timelines for tenant {tenant_id}"

    timeline_dirs = list(timelines_dir.iterdir())
    assert (
        timeline_dirs == initial_timeline_dirs
    ), "pageserver should clean its temp timeline files on timeline creation failure"


def test_timeline_create_break_after_uninit_mark(neon_env_builder: NeonEnvBuilder):
    env = neon_env_builder.init_start()
    pageserver_http = env.pageserver.http_client()

    tenant_id = env.initial_tenant

    timelines_dir = env.pageserver.timeline_dir(tenant_id)
    old_tenant_timelines = env.neon_cli.list_timelines(tenant_id)
    initial_timeline_dirs = list(timelines_dir.iterdir())

    # Introduce failpoint when creating a new timeline uninit mark, before any other files were created
    pageserver_http.configure_failpoints(("after-timeline-uninit-mark-creation", "return"))
    with pytest.raises(Exception, match="after-timeline-uninit-mark-creation"):
        _ = env.neon_cli.create_timeline("test_timeline_create_break_after_uninit_mark", tenant_id)

    # Creating the timeline didn't finish. The other timelines on tenant should still be present and work normally.
    # "New" timeline is not present in the list, allowing pageserver to retry the same request
    new_tenant_timelines = env.neon_cli.list_timelines(tenant_id)
    assert (
        new_tenant_timelines == old_tenant_timelines
    ), f"Pageserver after restart should ignore non-initialized timelines for tenant {tenant_id}"

    timeline_dirs = list(timelines_dir.iterdir())
    assert (
        timeline_dirs == initial_timeline_dirs
    ), "pageserver should clean its temp timeline files on timeline creation failure"
