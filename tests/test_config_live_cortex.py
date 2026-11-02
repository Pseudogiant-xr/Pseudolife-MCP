"""Live Console policies reach the resident cortex before the next write."""
from threading import Lock
from types import SimpleNamespace

import pytest
import torch

from pseudolife_memory.memory.cortex import CortexStore
from pseudolife_memory.memory.slots import Slot
from pseudolife_memory.utils.config import AppConfig
from pseudolife_memory.web.config_io import write_config


@pytest.fixture
def service(tmp_path, monkeypatch):
    monkeypatch.delenv("PSEUDOLIFE_MCP_CONFIG", raising=False)
    config = AppConfig()
    cc = config.memory.cortex
    return SimpleNamespace(config=config, data_dir=tmp_path, _lock=Lock(),
                           _cortex=CortexStore(protect_provenance=cc.protect_provenance,
                                               supersede_confidence_margin=cc.supersede_confidence_margin))


@pytest.mark.parametrize("field,value,support,confidence", [
    ("protect_provenance", False, "agent", 0.9),
    ("supersede_confidence_margin", 0.5, "user", 0.5),
])
def test_applied_policy_changes_the_next_fact_write(service, field, value, support, confidence):
    embedding = torch.tensor([1., 0., 0., 0.])
    service._cortex.write_fact(Slot("fixture", "host", "old"), embedding,
                               support="user", confidence=0.9, hlc=(1, 0))
    path = f"memory.cortex.{field}"
    result = write_config(service, {path: value})
    assert result["applied"] == [path]
    assert result["restart_required"] == []
    written = service._cortex.write_fact(Slot("fixture", "host", "new"), embedding,
                                         support=support, confidence=confidence, hlc=(2, 0))
    assert written.action == "superseded"
    assert service._cortex.lookup("fixture", "host").value == "new"


def test_policy_written_before_initialization_is_available_to_new_cortex(service):
    service._cortex = None
    patch = {"memory.cortex.protect_provenance": False,
             "memory.cortex.supersede_confidence_margin": 0.5}
    assert write_config(service, patch)["applied"] == list(patch)
    cc = service.config.memory.cortex
    resident = CortexStore(protect_provenance=cc.protect_provenance,
                           supersede_confidence_margin=cc.supersede_confidence_margin)
    assert resident.protect_provenance is False
    assert resident.supersede_confidence_margin == 0.5


def test_cold_file_hydration_keeps_applied_policy_over_snapshot(service, monkeypatch):
    from pseudolife_memory import service as service_module

    # Exercise the actual snapshot load and service hydration, without a model
    # or unrelated CMS persistence. The snapshot predates the Console edit.
    embedding = torch.tensor([1., 0., 0., 0.])
    service._cortex.write_fact(Slot("fixture", "host", "old"), embedding,
                               support="user", confidence=0.9, hlc=(1, 0))
    snapshot = service.data_dir / "cortex.pt"
    service._cortex.save(snapshot)
    service._cortex = None
    service._storage = None
    service._cortex_path = lambda: snapshot
    service._initialize_dream_tracking = lambda: None
    service._refuse_on_stale_hydrated_dims = lambda: None
    monkeypatch.setattr(service_module, "ContinuumMemorySystem",
                        lambda *a, **kw: SimpleNamespace(load=lambda path: None))
    patch = {"memory.cortex.protect_provenance": False,
             "memory.cortex.supersede_confidence_margin": 0.5}
    assert write_config(service, patch)["applied"] == list(patch)
    service_module.MemoryService._hydrate_resident_stores(service)
    assert service._cortex.supersede_confidence_margin == 0.5
    assert service._cortex.protect_provenance is False
    assert service._cortex.lookup("fixture", "host").value == "old"
    written = service._cortex.write_fact(Slot("fixture", "host", "new"), embedding,
                                         support="agent", confidence=0.5, hlc=(2, 0))
    assert written.action == "superseded"


def test_failed_save_changes_neither_configuration_nor_resident_policy(service, monkeypatch):
    def fail(*args):
        raise OSError("fixture replace failure")
    monkeypatch.setattr("pseudolife_memory.web.config_io.os.replace", fail)
    with pytest.raises(OSError, match="fixture replace failure"):
        write_config(service, {"memory.cortex.protect_provenance": False})
    assert service.config.memory.cortex.protect_provenance is True
    assert service._cortex.protect_provenance is True
