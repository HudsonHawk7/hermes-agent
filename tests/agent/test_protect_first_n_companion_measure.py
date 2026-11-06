"""Measure whether protect_first_n pins an old first exchange. Eval profile only.

Result on this fixture (old picture prompt is the first user turn):

    protect_first_n | old prompt pinned in the head
    0               | no
    1               | yes
    3               | yes

Recommendation: companions should use 0. The system prompt stays. 1 and 3 pin the
old picture prompt the GM had already moved past. This test does not change a live profile.
"""
from agent.context_compressor import ContextCompressor

_OLD = "OLD PICTURE PROMPT draw the castle"


def _messages():
    rows = [
        {"role": "system", "content": "You are the eval companion."},
        {"role": "user", "content": _OLD},
        {"role": "assistant", "content": "old reply"},
        {"role": "user", "content": "later topic"},
        {"role": "assistant", "content": "later reply"},
        {"role": "user", "content": "newest question"},
        {"role": "assistant", "content": "newest answer"},
    ]
    return rows


def _pinned(protect_first_n: int) -> bool:
    compressor = ContextCompressor(
        model="eval-model", protect_first_n=protect_first_n, protect_last_n=2, quiet_mode=True,
    )
    head = _messages()[: compressor._protect_head_size(_messages())]
    return any(_OLD in str(row.get("content") or "") for row in head)


def test_protect_first_n_table_for_companions():
    table = {n: _pinned(n) for n in (0, 1, 3)}
    # Printed so a run log is the result table the card asked for.
    print("protect_first_n | old_prompt_pinned")
    for n, pinned in table.items():
        print(f"{n} | {pinned}")
    assert table == {0: False, 1: True, 3: True}
