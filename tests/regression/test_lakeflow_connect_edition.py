"""Lakeflow Connect passes the DLT edition in the stored function's edition slot."""
from types import SimpleNamespace

import pytest

from app.routes.calculate import lakeflow_connect_calc
from app.routes.calculate.lakeflow_connect_calc import (
    calculate_lakeflow_connect_cost,
)
from app.routes.calculate.schemas import LakeflowConnectCalculationRequest


# Live Azure eastus PREMIUM values for a 730-hour Standard_DS3_v2 gateway.
GATEWAY_DBUS = 547.5
DLT_ADVANCED_RATE = 0.54
GATEWAY_VM_COST = 213.89


def _row(dbu_price=DLT_ADVANCED_RATE, dbu_per_month=GATEWAY_DBUS, vm_cost=0.0):
    return SimpleNamespace(
        dbu_per_hour=dbu_per_month / 730,
        hours_per_month=730.0,
        dbu_per_month=dbu_per_month,
        dbu_price=dbu_price,
        dbu_cost_per_month=dbu_per_month * dbu_price,
        driver_vm_cost_per_hour=vm_cost / 730 if vm_cost else 0.0,
        worker_vm_cost_per_hour=0.0,
        total_vm_cost_per_hour=vm_cost / 730 if vm_cost else 0.0,
        driver_vm_cost_per_month=vm_cost,
        total_worker_vm_cost_per_month=0.0,
        vm_cost_per_month=vm_cost,
        cost_per_month=dbu_per_month * dbu_price + vm_cost,
    )


def _resolve_sku(_db, workload_type, serverless_enabled, photon_enabled,
                 dlt_edition, _dbsql_warehouse_type, _fmapi_provider):
    """Mirror of lakemeter.get_product_type_for_pricing()'s DLT branch."""
    assert workload_type == "DLT"
    if serverless_enabled:
        return "JOBS_SERVERLESS_COMPUTE"
    edition = (dlt_edition or "").upper() or "CORE"
    sku = f"DLT_{edition}_COMPUTE"
    return f"{sku}_(PHOTON)" if photon_enabled else sku


@pytest.fixture
def calls(monkeypatch):
    """Record every stored-function parameter set the endpoint sends."""
    recorded = []

    def _call(_db, params):
        recorded.append(params)
        # The gateway is the only call that passes an instance type.
        is_gateway = params["p8"] is not None
        return _row(vm_cost=GATEWAY_VM_COST if is_gateway else 0.0)

    for name in ("validate_cloud", "validate_region", "validate_tier"):
        monkeypatch.setattr(
            lakeflow_connect_calc, name, lambda *a, **k: None
        )
    monkeypatch.setattr(
        lakeflow_connect_calc, "call_calculate_line_item_costs", _call
    )
    monkeypatch.setattr(
        lakeflow_connect_calc, "get_product_type_for_pricing", _resolve_sku
    )
    return recorded


def _request(**overrides):
    values = {
        "cloud": "azure",
        "region": "eastus",
        "tier": "PREMIUM",
        "hours_per_month": 730,
    }
    values.update(overrides)
    return LakeflowConnectCalculationRequest(**values)


def _run(**overrides):
    response = calculate_lakeflow_connect_cost(_request(**overrides), db=None)
    # The endpoint swallows exceptions into a success=False payload, so a
    # broken run must fail here rather than skip the assertions below.
    assert response["success"] is True, response
    return response["data"]


class TestEditionSlot:
    def test_gateway_prices_as_advanced_not_core(self, calls):
        data = _run(gateway_enabled=True, gateway_instance_type="Standard_DS3_v2")

        dbu_rows = [r for r in data["sku_breakdown"] if r["type"] == "dbu"]
        gateway_row = dbu_rows[-1]
        assert gateway_row["sku"] == "DLT_ADVANCED_COMPUTE"
        assert gateway_row["cost"] == pytest.approx(
            GATEWAY_DBUS * DLT_ADVANCED_RATE
        )
        assert data["gateway"]["total_cost"] == pytest.approx(
            GATEWAY_DBUS * DLT_ADVANCED_RATE + GATEWAY_VM_COST
        )

    def test_edition_goes_to_p7_and_dbsql_slot_stays_empty(self, calls):
        _run(gateway_enabled=True, gateway_instance_type="Standard_DS3_v2")

        pipeline, gateway = calls
        assert pipeline["p7"] == "ADVANCED"
        assert gateway["p7"] == "ADVANCED"
        assert pipeline["p18"] is None
        assert gateway["p18"] is None

    @pytest.mark.parametrize("edition", ("CORE", "PRO", "ADVANCED"))
    def test_requested_edition_reaches_the_pipeline(self, calls, edition):
        _run(dlt_edition=edition)

        assert calls[0]["p7"] == edition

    def test_pipeline_stays_on_the_serverless_sku(self, calls):
        """Serverless DLT ignores the edition, so its SKU must not change."""
        skus = set()
        for edition in ("CORE", "PRO", "ADVANCED"):
            calls.clear()
            skus.add(_run(dlt_edition=edition)["sku_type"])
        assert skus == {"JOBS_SERVERLESS_COMPUTE"}
