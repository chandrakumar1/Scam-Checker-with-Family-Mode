import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app.analyzer import cli, llm
from app.analyzer.llm import LLMAnalysis

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CLEAR = LLMAnalysis(
    verdict="no_obvious_red_flags",
    scam_type="none",
    red_flags=[],
    explanation="Nothing obvious.",
    what_to_do=[],
)


def fake_llm(result):
    async def fake_analyze(**kwargs):
        return result

    return fake_analyze


def run_cli(capsys, *args) -> dict:
    assert cli.main(list(args)) == 0
    return json.loads(capsys.readouterr().out)


def test_cli_prints_structured_result(monkeypatch, capsys):
    monkeypatch.setattr(llm, "analyze", fake_llm(CLEAR))
    out = run_cli(capsys, "Please share the OTP you received")
    assert out["verdict"] == "likely_scam"
    assert out["llm_verdict"] == "no_obvious_red_flags"
    assert out["rule_hits"][0]["rule"] == "otp_request"
    assert set(out) >= {"verdict", "rule_hits", "red_flags", "explanation", "what_to_do", "llm_failed"}


def test_cli_llm_failure_is_suspicious(monkeypatch, capsys):
    monkeypatch.setattr(llm, "analyze", fake_llm(None))
    out = run_cli(capsys, "Hello, how are you?")
    assert out["verdict"] == "suspicious"
    assert out["llm_failed"] is True


def test_cli_passes_language_and_image(monkeypatch, capsys, tmp_path):
    seen = {}

    async def fake_analyze(**kwargs):
        seen.update(kwargs)
        return CLEAR

    monkeypatch.setattr(llm, "analyze", fake_analyze)
    image = tmp_path / "shot.png"
    image.write_bytes(b"\x89PNG\r\n\x1a\nxyz")
    out = run_cli(capsys, "नमस्ते", "--lang", "hi", "--image", str(image))
    assert seen == {"text": "नमस्ते", "image_bytes": b"\x89PNG\r\n\x1a\nxyz", "language": "hi"}
    assert out["verdict"] == "no_obvious_red_flags"


def test_cli_requires_input():
    with pytest.raises(SystemExit) as exc:
        cli.main([])
    assert exc.value.code == 2


def test_cli_module_runs_without_api_key():
    """`python -m app.analyzer.cli` end to end, with the LLM disabled."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "LLM_"))}
    env["LLM_PROVIDER"] = "none"
    proc = subprocess.run(
        [sys.executable, "-m", "app.analyzer.cli", "Install AnyDesk and share the OTP"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    out = json.loads(proc.stdout)
    assert out["verdict"] == "likely_scam"
    assert out["llm_failed"] is True
    assert {h["rule"] for h in out["rule_hits"]} == {"remote_access_app", "otp_request"}
