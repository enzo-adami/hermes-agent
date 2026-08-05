from __future__ import annotations

import hermes_cli.oneshot as oneshot


def test_normalize_skills_accepts_repeated_and_comma_separated_values():
    assert oneshot._normalize_skills(["alpha,beta", "alpha", " gamma "]) == [
        "alpha",
        "beta",
        "gamma",
    ]


def test_run_oneshot_forwards_preloaded_skill_prompt(monkeypatch, capsys):
    seen = {}

    monkeypatch.setattr(oneshot, "declare_stateless_channel", lambda: None)
    monkeypatch.setattr(
        oneshot,
        "_prepare_preloaded_skills",
        lambda skills: ("ACTIVE SKILL BODY", None),
    )

    def fake_run_agent(prompt, **kwargs):
        seen["prompt"] = prompt
        seen.update(kwargs)
        return "ok", {"completed": True}

    monkeypatch.setattr(oneshot, "_run_agent", fake_run_agent)

    assert oneshot.run_oneshot("hello", skills=["cloud-cognition"]) == 0
    assert capsys.readouterr().out == "ok\n"
    assert seen["prompt"] == "hello"
    assert seen["ephemeral_system_prompt"] == "ACTIVE SKILL BODY"


def test_run_oneshot_rejects_missing_preloaded_skill(monkeypatch, capsys):
    monkeypatch.setattr(oneshot, "declare_stateless_channel", lambda: None)
    monkeypatch.setattr(
        oneshot,
        "_prepare_preloaded_skills",
        lambda skills: ("", "hermes -z: unknown or disabled skill(s): missing\n"),
    )

    assert oneshot.run_oneshot("hello", skills=["missing"]) == 2
    assert "unknown or disabled skill" in capsys.readouterr().err
