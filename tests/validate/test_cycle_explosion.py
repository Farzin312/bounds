
import pytest
from pathlib import Path
from bounds.models import SubsystemCompact, Consumes, RootManifest
from bounds.validate.checks import check_cycles, CheckContext

def test_cycle_explosion_reporting():
    # god -> {a1..a10} -> {b1..b10} -> god = 100 cycles
    subsystems = {}
    god_consumes = [Consumes(subsystem=f"a{i}") for i in range(10)]
    subsystems["god"] = SubsystemCompact(name="god", consumes=god_consumes)
    
    for i in range(10):
        a_consumes = [Consumes(subsystem=f"b{j}") for j in range(10)]
        subsystems[f"a{i}"] = SubsystemCompact(name=f"a{i}", consumes=a_consumes)
        for j in range(10):
            if f"b{j}" not in subsystems:
                b_consumes = [Consumes(subsystem="god")]
                subsystems[f"b{j}"] = SubsystemCompact(name=f"b{j}", consumes=b_consumes)

    ctx = CheckContext(
        project_root=Path("."),
        root=RootManifest(),
        subsystems=subsystems,
        extracts={},
        file_owner={},
        dirty=set(subsystems.keys())
    )
    
    issues = check_cycles(ctx)
    
    # It should report 10 individual cycles + 1 rollup issue = 11 total issues
    assert len(issues) == 11
    assert any("more cycles" in i.message for i in issues)
    
    # Check for bottleneck reporting
    rollup = next(i for i in issues if "more cycles" in i.message)
    assert "top bottlenecks" in rollup.message
    # In this specific graph (god -> a{i} -> b{j} -> god), 
    # 'b{j} -> god' should be a bottleneck as it's part of many cycles.
    assert "->god" in rollup.message
    assert "docs/troubleshooting-ci.md" in rollup.fix
