"""FastAPI adapter for authenticated records, jobs and employee routines.

FastAPI is imported lazily so the deterministic core and replay tests remain
dependency-free.  This adapter cannot sign, broadcast, deploy, or accept raw
wallet credentials.
"""

from datetime import datetime, timedelta

from economic_machine.values import MachineError, utc

from .auth import bearer_token

CUSTOMER_JOB_KINDS = {"RECONCILE_POSITION", "REFRESH_PLAN", "MONITOR_POLICY",
                      "REVIEW_PERFORMANCE"}
CUSTOMER_WRITABLE_RECORD_KINDS = {"CONVERSATION_DRAFT", "USER_NOTE"}


def _public_job(job):
    return {key: value for key, value in job.items()
            if key not in {"lease_token", "lease_owner", "account_key"}}


def create_app(*, session_verifier, repository, clock, product_service=None,
               web_root=None, demo_story=None):
    try:
        from fastapi import FastAPI, Header, HTTPException
    except ImportError as exc:
        raise RuntimeError("install the service optional dependencies to run FastAPI") from exc

    app = FastAPI(title="GWDC Economic Service", version="0.9.0")

    def context(authorization):
        try:
            return session_verifier.authenticate(bearer_token(authorization))
        except MachineError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def call(action):
        try:
            return action()
        except MachineError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/healthz")
    def healthz():
        try:
            storage = repository.health()
        except Exception as exc:
            raise HTTPException(status_code=503,
                                detail="storage health check failed") from exc
        if not storage["journal_integrity"]:
            raise HTTPException(status_code=503,
                                detail="storage journal integrity failed")
        return {"status": "ok", "execution_authority": "NONE", "storage": storage}

    @app.get("/v1/records/{record_kind}/{record_id}")
    def get_record(record_kind: str, record_id: str,
                   authorization: str = Header(alias="Authorization")):
        ctx = context(authorization)
        return call(lambda: repository.get_record(ctx.scope, record_kind, record_id))

    @app.put("/v1/records/{record_kind}/{record_id}")
    def put_record(record_kind: str, record_id: str, request: dict,
                   authorization: str = Header(alias="Authorization")):
        ctx = context(authorization)
        if (set(request) != {"expected_version", "body"}
                or record_kind not in CUSTOMER_WRITABLE_RECORD_KINDS):
            raise HTTPException(status_code=422,
                                detail="unsupported or incomplete writable record")
        return call(lambda: repository.put_record(ctx.scope, record_kind, record_id,
            request["body"], expected_version=request["expected_version"], at=utc(clock())))

    @app.post("/v1/jobs")
    def enqueue_job(request: dict,
                    authorization: str = Header(alias="Authorization")):
        ctx = context(authorization)
        required = {"role", "routine_id", "job_kind", "subject_id", "dependency_hash",
                    "payload", "ttl_seconds", "max_attempts", "priority"}
        if set(request) != required or request["job_kind"] not in CUSTOMER_JOB_KINDS:
            raise HTTPException(status_code=422, detail="unsupported or incomplete customer job")
        now = datetime.fromisoformat(utc(clock()))
        ttl = request["ttl_seconds"]
        if type(ttl) is not int or not 30 <= ttl <= 3600:
            raise HTTPException(status_code=422, detail="job TTL outside API policy")
        return call(lambda: _public_job(repository.enqueue_job(ctx.scope,
            role=request["role"], routine_id=request["routine_id"],
            job_kind=request["job_kind"], subject_id=request["subject_id"],
            dependency_hash=request["dependency_hash"], payload=request["payload"],
            not_before=now.isoformat(),
            expires_at=(now + timedelta(seconds=ttl)).isoformat(),
            max_attempts=request["max_attempts"], priority=request["priority"])))

    @app.get("/v1/jobs")
    def list_jobs(authorization: str = Header(alias="Authorization")):
        ctx = context(authorization)
        return call(lambda: [_public_job(item) for item in repository.list_jobs(ctx.scope)])

    @app.put("/v1/routines/{routine_id}")
    def put_routine(routine_id: str, request: dict,
                    authorization: str = Header(alias="Authorization")):
        ctx = context(authorization)
        required = {"role", "responsibility", "status", "interval_seconds",
                    "next_due_at", "dependency_hash"}
        if set(request) != required:
            raise HTTPException(status_code=422, detail="exact routine request fields required")
        return call(lambda: repository.put_routine(ctx.scope, routine_id=routine_id,
            role=request["role"], responsibility=request["responsibility"],
            status=request["status"], interval_seconds=request["interval_seconds"],
            next_due_at=request["next_due_at"],
            dependency_hash=request["dependency_hash"], at=utc(clock())))

    @app.get("/v1/routines")
    def list_routines(authorization: str = Header(alias="Authorization")):
        ctx = context(authorization)
        return call(lambda: repository.list_routines(ctx.scope))

    if demo_story is not None:
        from .product_workspace import normalize_product_story

        demo_story = normalize_product_story(demo_story)

        @app.get("/api/demo/story")
        def read_demo_story():
            return demo_story

    if product_service is not None:
        @app.get("/v1/workspaces/{story_id}")
        def read_workspace(story_id: str,
                           authorization: str = Header(alias="Authorization")):
            ctx = context(authorization)
            return call(lambda: product_service.read(ctx, story_id))

        @app.post("/v1/workspaces/{story_id}/decisions")
        def decide_workspace(story_id: str, request: dict,
                             authorization: str = Header(alias="Authorization")):
            ctx = context(authorization)
            required = {"decision", "story_revision", "plan_hash", "approval_hash",
                        "expected_decision_version"}
            if set(request) != required:
                raise HTTPException(status_code=422,
                                    detail="exact workspace decision fields required")
            return call(lambda: product_service.decide(ctx, story_id, request))

    if web_root is not None:
        from pathlib import Path

        from fastapi.staticfiles import StaticFiles

        root = Path(web_root).resolve()
        if not root.is_dir() or not (root / "index.html").is_file():
            raise RuntimeError("finance service web root is incomplete")
        app.mount("/", StaticFiles(directory=root, html=True), name="product-web")

    return app
