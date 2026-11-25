"""Where an embedding model ranks the right skill, by embedding alone (RM45, RM47; spec 0.74)."""

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location(
    "probe_module", ROOT / "scripts/guidance_embedding_probe.py"
)
probe = importlib.util.module_from_spec(spec)
sys.modules["probe_module"] = probe
spec.loader.exec_module(probe)

DATA = dict(
    splits=dict(
        holdout=[
            dict(
                family="f",
                skills=[
                    dict(
                        name="pdf", description="merge pdf", en="merge pdf files", zh="合并PDF文件"
                    ),
                    dict(
                        name="deploy", description="deploy service", en="deploy it", zh="部署服务"
                    ),
                ],
            )
        ]
    )
)

VECTORS = {
    "merge pdf": [1.0, 0.0],
    "deploy service": [0.0, 1.0],
    "merge pdf files": [0.9, 0.1],
    "deploy it": [0.2, 0.8],
    "合并PDF文件": [0.6, 0.8],  # nearer "deploy service": ranked second
    "部署服务": [0.1, 0.9],
}


def fake_embed(texts):
    return [VECTORS[t.removeprefix("q: ").removeprefix("d: ")] for t in texts]


def test_a_chinese_prompt_is_pure_only_without_a_latin_word():
    assert probe.group_of("部署服务") == "zh_pure"
    assert probe.group_of("合并PDF文件") == "zh_mixed"
    # One Latin letter beside a digit, as in "S3", is not a word.
    assert probe.group_of("上传到S3") == "zh_pure"


def test_each_group_reports_rank_and_cosine_of_the_right_skill():
    result = probe.probe(DATA, "holdout", fake_embed)
    en, pure, mixed = result["en"], result["zh_pure"], result["zh_mixed"]
    assert (en["prompts"], en["rank_1"], en["top_3"]) == (2, 2, 2)
    assert (pure["prompts"], pure["rank_1"]) == (1, 1)
    assert (mixed["prompts"], mixed["rank_1"], mixed["top_3"]) == (1, 0, 1)
    assert mixed["right_cosine"]["median"] == pytest.approx(0.6)
    assert set(en["right_cosine"]) == {"min", "median", "max"}


def test_prefixes_go_on_the_prompt_and_on_the_description():
    seen = []

    def embed(texts):
        seen.extend(texts)
        return fake_embed(texts)

    probe.probe(DATA, "holdout", embed, query_prefix="q: ", document_prefix="d: ")
    assert "d: merge pdf" in seen and "q: 部署服务" in seen


def test_the_command_reads_the_data_and_writes_json(tmp_path, monkeypatch):
    data, out = tmp_path / "d.json", tmp_path / "o.json"
    data.write_text(json.dumps(DATA), "utf-8")
    monkeypatch.setattr(probe, "http_embedder", lambda url, model: fake_embed)
    probe.main(["--data", str(data), "--url", "http://127.0.0.1:1", "--output", str(out)])
    saved = json.loads(out.read_text("utf-8"))
    assert saved["split"] == "holdout" and saved["groups"]["en"]["rank_1"] == 2


def test_only_loopback_endpoints_are_accepted():
    """The probe sends prompts to the model, so it never sends them off the machine (RM01)."""
    with pytest.raises(SystemExit):
        probe.main(["--url", "https://example.com"])


def test_the_probe_never_sends_prompts_through_a_proxy(monkeypatch):
    """A proxy from the environment would take the prompts off the machine (RM01)."""
    seen = []

    class Opener:
        def open(self, request, timeout):
            raise RuntimeError("stop")

    def build_opener(*handlers):
        seen.extend(handlers)
        return Opener()

    monkeypatch.setattr(probe.urllib.request, "build_opener", build_opener)
    with pytest.raises(RuntimeError, match="stop"):
        probe.http_embedder("http://127.0.0.1:1", "m")(["text"])
    proxies = [h for h in seen if isinstance(h, probe.urllib.request.ProxyHandler)]
    assert proxies and proxies[0].proxies == {}


def test_any_loopback_address_is_accepted():
    for url in ("http://127.0.0.2:1", "http://[::1]:1", "http://localhost:1"):
        with pytest.raises(OSError):  # accepted, then the empty port refuses the connection
            probe.main(["--url", url, "--output", "/dev/null"])
