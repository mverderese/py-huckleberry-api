"""Unit tests for listing history entries with ids and deleting them."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from google.cloud.firestore import DELETE_FIELD

from huckleberry_api import HuckleberryAPI


class _Snapshot:
    def __init__(self, doc_id: str, payload: dict[str, object] | None) -> None:
        self.id = doc_id
        self._payload = payload
        self.exists = payload is not None

    def to_dict(self) -> dict[str, object] | None:
        return self._payload


async def _aiter(items):
    for item in items:
        yield item


class _Query:
    def __init__(self, history: "_History", multi: bool) -> None:
        self._history = history
        self._multi = multi

    def where(self, *, filter) -> "_Query":
        return _Query(self._history, multi=filter.field_path == "multi" or self._multi)

    def order_by(self, _field: str) -> "_Query":
        return self

    def stream(self):
        docs = [
            _Snapshot(doc_id, data)
            for doc_id, data in self._history.docs.items()
            if bool(data.get("multi")) == self._multi
        ]
        return _aiter(docs)


class _DocRef:
    def __init__(self, history: "_History", doc_id: str) -> None:
        self._history = history
        self._doc_id = doc_id

    async def get(self) -> _Snapshot:
        return _Snapshot(self._doc_id, self._history.docs.get(self._doc_id))

    async def update(self, changes: dict[str, object]) -> None:
        self._history.updates.append((self._doc_id, changes))

    async def delete(self) -> None:
        self._history.deleted.append(self._doc_id)


class _History:
    def __init__(self, docs: dict[str, dict[str, object]]) -> None:
        self.docs = docs
        self.updates: list[tuple[str, dict[str, object]]] = []
        self.deleted: list[str] = []

    def where(self, *, filter) -> _Query:
        return _Query(self, multi=False).where(filter=filter)

    def document(self, doc_id: str) -> _DocRef:
        return _DocRef(self, doc_id)


class _Client:
    def __init__(self, histories: dict[tuple[str, str], _History]) -> None:
        self._histories = histories
        self._path: list[str] = []

    def collection(self, name: str):
        client = self

        class _Root:
            def document(self, child_uid: str):
                assert child_uid == "child"

                class _ChildDoc:
                    def collection(self, sub: str) -> _History:
                        return client._histories[(name, sub)]

                return _ChildDoc()

        return _Root()


START = datetime(2026, 10, 19, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 20, 0, 0, tzinfo=timezone.utc)
IN_RANGE = START.timestamp() + 3600

DIAPER_HISTORY = {
    "solo": {"mode": "pee", "start": IN_RANGE, "offset": 240},
    "batch": {
        "multi": True,
        "data": {
            "k1": {"mode": "both", "start": IN_RANGE + 60, "offset": 240},
            "old": {"mode": "dry", "start": START.timestamp() - 86400, "offset": 240},
        },
    },
}


def _api(websession, monkeypatch, histories):
    api = HuckleberryAPI(email="test", password="test", timezone="UTC", websession=websession)
    monkeypatch.setattr(api, "_get_firestore_client", AsyncMock(return_value=_Client(histories)))
    return api


async def test_list_entries_with_ids_covers_standalone_and_multi_rows(websession, monkeypatch) -> None:
    api = _api(websession, monkeypatch, {("diaper", "intervals"): _History(DIAPER_HISTORY)})

    entries = await api.list_entries_with_ids("child", "diaper", START, END)

    assert [(entry_id, entry.mode) for entry_id, entry in entries] == [("solo", "pee"), ("batch/k1", "both")]


async def test_list_entries_with_ids_reads_health_data_subcollection(websession, monkeypatch) -> None:
    health = _History(
        {
            "t1": {
                "mode": "temperature",
                "start": IN_RANGE,
                "lastUpdated": IN_RANGE,
                "offset": 0,
                "amount": 37.2,
                "units": "C",
            }
        }
    )
    api = _api(websession, monkeypatch, {("health", "data"): health})

    entries = await api.list_entries_with_ids("child", "health", START, END)

    assert [entry_id for entry_id, _ in entries] == ["t1"]


async def test_delete_standalone_entry_deletes_document(websession, monkeypatch) -> None:
    history = _History(dict(DIAPER_HISTORY))
    api = _api(websession, monkeypatch, {("diaper", "intervals"): history})

    await api.delete_entry("child", "diaper", "solo")

    assert history.deleted == ["solo"] and history.updates == []


async def test_delete_multi_entry_removes_only_that_key(websession, monkeypatch) -> None:
    history = _History(dict(DIAPER_HISTORY))
    api = _api(websession, monkeypatch, {("diaper", "intervals"): history})

    await api.delete_entry("child", "diaper", "batch/k1")

    assert history.deleted == []
    assert history.updates == [("batch", {"data.k1": DELETE_FIELD})]


@pytest.mark.parametrize("entry_id", ["missing", "batch/missing", "solo/k1", "batch", ""])
async def test_delete_rejects_unknown_or_ambiguous_ids(websession, monkeypatch, entry_id) -> None:
    history = _History(dict(DIAPER_HISTORY))
    api = _api(websession, monkeypatch, {("diaper", "intervals"): history})

    with pytest.raises(ValueError):
        await api.delete_entry("child", "diaper", entry_id)

    assert history.deleted == [] and history.updates == []
