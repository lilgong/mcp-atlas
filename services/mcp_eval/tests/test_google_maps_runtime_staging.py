"""Guard both steps required to ship the Google Maps adapter in the runtime."""
import shlex
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "scripts"))
from build_atlas_runtime import stage_build_context


def test_google_maps_adapter_is_staged_and_copied_into_runtime():
    adapter = "src/agent_environment/google_maps_compat.cjs"
    with tempfile.TemporaryDirectory() as raw:
        context = Path(raw)
        stage_build_context(context)
        assert (context / adapter).is_file()
        dockerfile = (context / "Dockerfile").read_text().replace("\\\n", " ")
        copies = [shlex.split(line) for line in dockerfile.splitlines()
                  if line.startswith("COPY ")]
        assert any(adapter in line[1:-1] and line[-1] == "src/agent_environment/"
                   for line in copies), "Adapter staged but not copied into the runtime image"
