"""Model endpoints synthesized from lanes (no [models] section) must be usable by every
harness they are offered to: one endpoint per real model, ids without a harness's prefix,
cloud ids only for the CLI that can reach them, nothing from a switched-off or unavailable lane."""

from fleet.core.config import LaneSpec, LaneStep, models_from_lanes
from fleet.core.models import compute_compatibility
from fleet.core.router import Router
from fleet.plugins.harness_base import AiderHarness, GooseHarness, OpenCodeHarness

URL = "http://127.0.0.1:30001/v1"


def _lanes():
    return {
        "local_qwen": LaneSpec(cost="local", private=True, probe_url=URL,
                               steps=[LaneStep(harness="goose", model="qwen3.6-35b")]),
        "local_aider": LaneSpec(cost="local", private=True, probe_url=URL,
                                steps=[LaneStep(harness="aider", model="openai/qwen3.6-35b")]),
        "cloud_oc": LaneSpec(steps=[LaneStep(harness="opencode", model="opencode/some-free")]),
        "off": LaneSpec(enabled=False, steps=[LaneStep(harness="opencode", model="opencode/x")]),
    }


def test_one_endpoint_per_local_model_without_harness_prefix():
    models = models_from_lanes(_lanes())
    local = {n: m for n, m in models.items() if m.base_url == URL}
    assert list(local) == ["local_qwen"]
    assert local["local_qwen"].model_id == "qwen3.6-35b"


def test_switched_off_lane_gives_no_model():
    assert "off" not in models_from_lanes(_lanes())


def test_cloud_id_only_for_the_cli_that_can_reach_it():
    ep = models_from_lanes(_lanes())["cloud_oc"]
    assert compute_compatibility(OpenCodeHarness(), ep)[0]
    ok, reason = compute_compatibility(GooseHarness(), ep)
    assert not ok and "opencode" in reason


def test_goose_gets_the_plain_id_and_aider_its_own_prefix():
    ep = models_from_lanes(_lanes())["local_qwen"]
    assert GooseHarness().adapt_model(ep).env["GOOSE_MODEL"] == "qwen3.6-35b"
    args = AiderHarness().adapt_model(ep).args
    assert args[args.index("--model") + 1] == "openai/qwen3.6-35b"


def test_unavailable_models_are_not_ranked():
    lanes = _lanes()
    router = Router(lanes, [], unavailable=["cloud_oc"])
    pairs = router.rank_pairs({"opencode": OpenCodeHarness()}, models_from_lanes(lanes))
    assert all(p.model != "cloud_oc" for p in pairs)


def test_nvidia_lane_synthesized_endpoint_preserves_key_and_base_url():
    lanes = {
        "auto_nvidia_glm": LaneSpec(
            cost="free",
            provider="nvidia",
            key_env="OPENAI_API_KEY",
            key_file="nvidia-api-key",
            steps=[LaneStep(
                harness="aider",
                model="openai/z-ai/glm5",
                args=["--openai-api-base", "https://integrate.api.nvidia.com/v1"],
            )],
        ),
    }
    ep = models_from_lanes(lanes)["auto_nvidia_glm"]
    assert ep.key_env == "OPENAI_API_KEY"
    assert ep.key_file == "nvidia-api-key"
    assert ep.base_url == "https://integrate.api.nvidia.com/v1"
    assert compute_compatibility(AiderHarness(), ep) == (True, None)


def test_aider_mirrors_openai_key_to_aider_env(monkeypatch, tmp_path):
    import asyncio
    from fleet.plugins import harness_base

    captured_env = {}

    async def fake_exec(self, task, worktree_dir, timeout=600, model="", args=(),
                        pass_env=(), env=None, endpoint=None):
        captured_env.update(env or {})
        return {"harness": "aider", "exit_code": 0, "stdout": "Applied edit to foo.py\n", "stderr": ""}

    monkeypatch.setattr(harness_base.CliHarness, "execute_task", fake_exec)
    res = asyncio.run(
        AiderHarness().execute_task(
            {"text": "fix"},
            str(tmp_path),
            env={"OPENAI_API_KEY": "nvapi-real-token"},
        )
    )
    assert res["exit_code"] == 0
    assert captured_env["OPENAI_API_KEY"] == "nvapi-real-token"
    assert captured_env["AIDER_OPENAI_API_KEY"] == "nvapi-real-token"
