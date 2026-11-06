"""Foreground isolated app launch, browser interaction, API restart and cleanup."""
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[2]
WORK = ROOT.parent
sys.path.insert(0, str(ROOT / "apps/api/src"))


def ready(url, child):
    for _ in range(90):
        if child.poll() is not None:
            raise RuntimeError(f"Service exited {child.returncode}")
        try:
            with urlopen(url, timeout=1) as response:
                if response.status == 200:
                    return
        except Exception:
            time.sleep(0.25)
    raise RuntimeError("Service not ready")


def stop(child):
    if child is not None and child.poll() is None:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


with tempfile.TemporaryDirectory(prefix="mian-2-smoke-", dir=WORK) as tmp:
    isolated = Path(tmp)
    evidence = ROOT / "delivery/MIAN-2/evidence"
    evidence.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update(WIKI_AGENT_DINGTALK_MODE="mock", WIKI_AGENT_DATABASE_URL=f"sqlite:///{isolated}/db.sqlite",
               WIKI_AGENT_KNOWLEDGE_ROOT=str(isolated / "knowledge"),
               WIKI_AGENT_STORAGE_ROOT=str(isolated / "storage"),
               WIKI_AGENT_BOOTSTRAP_ADMIN_EMAIL="smoke@example.com",
               WIKI_AGENT_BOOTSTRAP_ADMIN_PASSWORD=secrets.token_urlsafe(20),
               WIKI_AGENT_CORS_ORIGINS="http://localhost:13082", SMOKE_DIR=str(evidence))
    env["SMOKE_PASSWORD"] = env["WIKI_AGENT_BOOTSTRAP_ADMIN_PASSWORD"]
    env["NEXT_PUBLIC_API_URL"] = "http://localhost:18082/api/v1"
    api = web = None
    with (evidence / "api.log").open("w") as api_log, (evidence / "web.log").open("w") as web_log:
        try:
            api_cmd = [sys.executable, "-m", "uvicorn", "wiki_agent.main:app", "--host", "127.0.0.1", "--port", "18082"]
            api = subprocess.Popen(api_cmd, cwd=ROOT, env=env, stdout=api_log, stderr=api_log)
            ready("http://localhost:18082/health", api)
            from wiki_agent.wiki.repository import WikiRepository
            repo = WikiRepository(isolated / "knowledge")
            old = "---\nid: old-c\ntype: source\ntitle: Existing C\ncreated: 2026-01-01T00:00:00Z\nupdated: 2026-01-01T00:00:00Z\n---\n\nExisting C stays unchanged.\n"
            repo.publish({"sources/existing-c.md": old}, base_commit=repo.head(), author_name="Smoke U",
                         author_email="smoke@example.com", message="Seed unrelated C")
            before = repo.head()
            old_history = repo.history("sources/existing-c.md")
            next_bin = ROOT / "apps/user-web/node_modules/next/dist/bin/next"
            web = subprocess.Popen(["node", str(next_bin), "dev", "--hostname", "127.0.0.1", "--port", "13082"],
                                   cwd=ROOT / "apps/user-web", env=env, stdout=web_log, stderr=web_log)
            ready("http://localhost:13082/login", web)
            driver = ROOT / "delivery/MIAN-2/smoke-browser.cjs"
            playwright = WORK / "local-tools/node_modules/playwright"
            subprocess.run(["node", str(driver), str(playwright), "import"], env=env, check=True)
            result = json.loads((evidence / "import-result.json").read_text())
            assert repo._run("diff", "--name-only", before, result["published_commit"]).stdout.splitlines() == ["wiki/" + result["page_path"]]
            assert repo.history("sources/existing-c.md") == old_history
            assert len(repo.list_pages()) == 2
            stop(api)
            api = subprocess.Popen(api_cmd, cwd=ROOT, env=env, stdout=api_log, stderr=api_log)
            ready("http://localhost:18082/health", api)
            subprocess.run(["node", str(driver), str(playwright), "restart"], env=env, check=True)
            assert repo.head() == result["published_commit"]
            (evidence / "git-diff.txt").write_text(repo._run("diff", before, result["published_commit"]).stdout)
            (evidence / "smoke-summary.json").write_text(json.dumps({"mode": "mock", "ports": [18082, 13082],
                "before": before, "after": repo.head(), "pages": repo.list_pages(), "old_c_unchanged": True,
                "api_restart_relogin_refresh": "pass", "browser": "pass", "models_configured": False}, indent=2))
            print("SIM browser smoke PASS; fresh login, exact A, one Git page, C unchanged, API restart+relogin+refresh")
        finally:
            stop(web)
            stop(api)
            print("Smoke API/Next processes stopped; isolated runtime data cleaned")
