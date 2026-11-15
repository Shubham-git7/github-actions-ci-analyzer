"""Drive the LIVE local stack in a real browser and capture screenshots plus a video of one full workflow.

Prereqs: the stack is up (./start_local.sh) and the sandbox is seeded (./start_local.sh seed). Needs Playwright:
    uv run --no-project --with playwright python scripts/capture_demo.py main --chrome "<path to a Chromium/Chrome binary>"
Phases:
    main   sign in as alice, analyze, wait for the approval gate, show self-approval refused, sign in as bob, approve, wait for the merge;
           then the Temporal UI history for that run. Writes docs/screenshots/live-*.png and numbered frames in docs/media/frames/
           (assemble them with scripts/make_gif.sh; Playwright's own video recorder needs a ffmpeg build we do not rely on)
    chaos  trigger a run, kill the worker mid-run, show 'worker offline', restart it, show the run finishing at the approval gate
It never touches anything except the local stack and what the app itself does (the sandbox repo named in .env)."""
import argparse
import json
import pathlib
import subprocess
import time

from playwright.sync_api import sync_playwright

ROOT = pathlib.Path(__file__).resolve().parent.parent
APP, TUI = "http://localhost:8080", "http://localhost:8233"


def env(key: str) -> str:
    for ln in (ROOT / ".env").read_text().splitlines():
        if ln.startswith(key + "="):
            return ln.split("=", 1)[1].split("#")[0].strip()
    return ""


def compose(*args: str) -> None:
    files = ["-f", "docker-compose.yml"] + (["-f", "compose.ollama.yml"] if env("LLM_BACKEND") == "ollama" else [])
    subprocess.run(["docker", "compose", *files, *args], cwd=ROOT, check=True, capture_output=True)


class Cap:
    def __init__(self, page, out: pathlib.Path, frames: pathlib.Path | None = None):
        self.page, self.out, self.t0, self.marks = page, out, time.time(), []
        self.frames, self.n = frames, 0
        if frames:
            frames.mkdir(parents=True, exist_ok=True)
            for old in frames.glob("*.jpg"):
                old.unlink()

    def frame(self) -> None:
        if self.frames:
            self.page.evaluate("window.scrollTo(0, 0)")
            self.n += 1
            self.page.screenshot(path=str(self.frames / f"{self.n:05d}.jpg"), type="jpeg", quality=82)

    def burst(self, seconds: float = 2.0, every: float = 0.35) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.frame()
            self.page.wait_for_timeout(int(every * 1000))

    def shot(self, name: str, full: bool = False) -> None:
        self.page.evaluate("window.scrollTo(0, 0)")
        self.page.wait_for_timeout(700)  # let animations settle
        self.page.screenshot(path=str(self.out / f"{name}.png"), full_page=full)
        print("shot", name)

    def mark(self, label: str) -> None:
        self.marks.append((label, round(time.time() - self.t0, 2)))

    def wait_status(self, text: str, timeout_s: int, every: float = 6.0) -> None:
        """Poll for a status badge, keeping one frame per `every` seconds (the GIF compresses the wait)."""
        self.mark(f"wait:{text}:start")
        end = time.time() + timeout_s
        js = "t => [...document.querySelectorAll('#detail .badge')].some(b => b.textContent.includes(t))"
        while not self.page.evaluate(js, text):
            if time.time() > end:
                raise TimeoutError(f"never saw status {text!r}")
            self.frame()
            self.page.wait_for_timeout(int(every * 1000))
        self.mark(f"wait:{text}:end")


def sign_in(page, who: str, cap: "Cap") -> None:
    page.click("#login")
    page.wait_for_selector("form[action='/oauth2/pick']")
    cap.burst(1.2)
    if who == "alice":
        page.screenshot(path=str(ROOT / "docs/screenshots/live-01-dev-signin.png"))
    page.click(f"form:has(input[value='{who}']) button")
    page.wait_for_selector("#who:not([hidden])")
    cap.burst(0.8)


def sign_out(page) -> None:
    page.click("#logout")
    page.wait_for_selector("#login:not([hidden])")


def phase_main(p, chrome: str, wait_s: int) -> None:
    shots, media = ROOT / "docs/screenshots", ROOT / "docs/media"
    media.mkdir(parents=True, exist_ok=True)
    b = p.chromium.launch(executable_path=chrome)
    ctx = b.new_context(viewport={"width": 1280, "height": 1560})
    page = ctx.new_page()
    c = Cap(page, shots, media / "frames")
    page.goto(APP)
    page.wait_for_selector("#login")
    c.shot("live-00-signed-out")
    c.burst(1.2)
    c.mark("signin_alice")
    sign_in(page, "alice", c)
    page.wait_for_selector("#newcard:not([hidden])")
    c.shot("live-02-alice-ready")
    c.burst(1.0)
    c.mark("analyze")
    page.click("#go")
    page.wait_for_selector("#runs .run")
    c.burst(2.5)
    c.shot("live-03-analyzing")
    c.wait_status("Awaiting approval", wait_s)
    c.mark("gate")
    c.shot("live-04-awaiting-approval")
    c.burst(2.0)
    sel = page.locator("#detail [data-act='approve']")
    assert sel.is_disabled(), "alice must not be able to approve her own run"
    c.shot("live-05-self-approval-blocked")
    c.burst(2.0)
    c.mark("signin_bob")
    sign_out(page)
    sign_in(page, "bob", c)
    page.wait_for_selector("#runs .run")
    page.click("#runs .run")
    page.wait_for_selector("#detail [data-act='approve']:not([disabled])")
    c.shot("live-06-bob-can-approve")
    c.burst(1.5)
    page.click("#detail [data-act='approve']")
    page.wait_for_selector("#dlg[open]")
    c.shot("live-07-approve-dialog")
    c.burst(1.5)
    page.click("#dok")
    c.wait_status("Fixed", 180, every=0.5)
    c.mark("done")
    c.burst(1.0)
    c.shot("live-08-fixed")
    c.burst(2.5)
    wfid = page.evaluate("document.querySelector('#runs .run').dataset.id")
    # Temporal UI: the same run as a durable workflow history
    t = ctx.new_page()
    t.set_viewport_size({"width": 1440, "height": 900})
    t.goto(f"{TUI}/namespaces/default/workflows/{wfid}")
    t.wait_for_timeout(6000)
    t.screenshot(path=str(shots / "live-09-temporal-history.png"))
    t.get_by_role("tab", name="Event History").first.click() if t.get_by_role("tab", name="Event History").count() else t.get_by_text("Event History").first.click()
    t.wait_for_timeout(3500)
    t.screenshot(path=str(shots / "live-10-temporal-event-history.png"))
    (media / "marks.json").write_text(json.dumps({"marks": c.marks, "wfid": wfid}))
    ctx.close()
    b.close()


def phase_chaos(p, chrome: str, wait_s: int) -> None:
    shots = ROOT / "docs/screenshots"
    b = p.chromium.launch(executable_path=chrome)
    ctx = b.new_context(viewport={"width": 1280, "height": 1120})
    page = ctx.new_page()
    c = Cap(page, shots)
    page.goto(APP)
    sign_in(page, "alice", c)
    page.click("#go")
    page.wait_for_selector("#runs .run")
    page.wait_for_timeout(8000)
    print("killing the worker mid-run")
    compose("kill", "worker")
    page.wait_for_selector(".toast.bad", timeout=30_000)
    page.wait_for_timeout(500)
    c.shot("live-11-worker-killed-run-safe")
    wfid = page.evaluate("document.querySelector('#runs .run').dataset.id")
    t = ctx.new_page()
    t.set_viewport_size({"width": 1440, "height": 900})
    t.goto(f"{TUI}/namespaces/default/workflows/{wfid}")
    t.wait_for_timeout(6000)
    t.screenshot(path=str(shots / "live-12-temporal-waiting-for-worker.png"))
    print("restarting the worker")
    compose("up", "-d", "worker")
    page.bring_to_front()
    c.wait_status("Awaiting approval", wait_s)
    c.shot("live-13-resumed-after-restart")
    ctx.close()
    b.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("phase", choices=["main", "chaos"])
    ap.add_argument("--chrome", required=True)
    ap.add_argument("--wait", type=int, default=900, help="seconds to wait for the model to finish")
    a = ap.parse_args()
    with sync_playwright() as pw:
        (phase_main if a.phase == "main" else phase_chaos)(pw, a.chrome, a.wait)
