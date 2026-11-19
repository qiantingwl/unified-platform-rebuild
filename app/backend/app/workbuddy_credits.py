"""Official credit-package observations, never reconstructed transactions."""
from datetime import datetime, timedelta, timezone
import time
import math
from .upstream import UpstreamError, amount

CST = timezone(timedelta(hours=8))


def timestamp(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value/1000 if value > 100000000000 else value
        return seconds if math.isfinite(seconds) and 0 < seconds < 253402300800 else None
    if isinstance(value, str) and value:
        try:
            date = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (date if date.tzinfo else date.replace(tzinfo=CST)).timestamp()
        except (ValueError, OverflowError, OSError):
            pass
    return None


def packages(data):
    response = data.get("Response", data.get("response", {})) if isinstance(data, dict) else {}
    nested = response.get("Data", response.get("data", {})) if isinstance(response, dict) else {}
    rows = nested.get("Accounts", nested.get("accounts")) if isinstance(nested, dict) else None
    if not isinstance(rows, list):
        raise UpstreamError("credit_packages_unavailable")
    result = []
    for row in rows:
        if not isinstance(row, dict):
            raise UpstreamError("invalid_credit_package")
        if row.get("CapacityUnit") and row["CapacityUnit"] != "credits":
            continue
        cycle = amount(row.get("CycleCapacitySize")) if row.get("CycleCapacitySize") is not None else None
        prefix = "CycleCapacity" if cycle is not None and cycle > 0 else "Capacity"
        remain, size, used = [amount(row.get(prefix+key)) if row.get(prefix+key) is not None else None for key in ("Remain", "Size", "Used")]
        # Unknown is retained as unknown; do not fill missing balances with 0.
        end_field = next((k for k in ("DeductionEndTime", "ExpiredTime", "PackageEndTime", "CycleEndTime") if row.get(k)), None)
        result.append({"name": row.get("PackageName"), "package_code": row.get("PackageCode"),
            "sub_product_code": row.get("SubProductCode"), "sub_product_name": row.get("SubProductName"),
            "remaining": remain, "size": size, "used": used, "cycle": prefix == "CycleCapacity",
            "expires_at": timestamp(row.get(end_field)) if end_field else None,
            "expiry_source": end_field, "created_at": timestamp(row.get("CreateTime")),
            "source": "official_package"})
    return result


async def observe(adapter, platform, credential_version):
    if platform not in {"WorkBuddy 国内", "WorkBuddy 国际"}:
        raise UpstreamError("credit_packages_unavailable", 422)
    account = adapter.c.get("account") or {}
    if account.get("enterpriseId") or account.get("enterprise_id"):
        raise UpstreamError("enterprise_credit_packages_unavailable", 422)
    now = datetime.now(CST)
    body = {"PageNumber": 1, "PageSize": 100, "ProductCode": "p_tcaca", "Status": [0, 3],
            "PackageEndTimeRangeBegin": now.strftime("%Y-%m-%d %H:%M:%S"),
            "PackageEndTimeRangeEnd": (now+timedelta(days=365*101)).strftime("%Y-%m-%d %H:%M:%S")}
    # Source 7864 uses no /v2 for international billing, /v2 for domestic.
    path = "/billing/meter/get-user-resource" if platform == "WorkBuddy 国际" else "/v2/billing/meter/get-user-resource"
    rows = []
    # Paginate upstream, do not quietly label the first 100 packages complete.
    for number in range(1, 101):
        data = await adapter.request("POST", adapter.base+path, adapter.headers(), {**body, "PageNumber": number})
        current = packages(data)
        rows.extend(current)
        response = data.get("Response", data.get("response", {}))
        nested = response.get("Data", response.get("data", {}))
        raw_rows = nested.get("Accounts", nested.get("accounts", []))
        if len(raw_rows) < body["PageSize"]:
            return {"status": "verified", "packages": rows, "observed_at": time.time(),
                    "credential_version": credential_version, "source": "official_packages", "transactions_available": False}
    raise UpstreamError("credit_package_page_limit")
