import math

import pytest

from tagbridge.config import TagConfig
from tagbridge.sources.modbus import (
    ModbusAddress,
    _TagPlan,
    decode_registers,
    encode_value,
    parse_address,
    plan_blocks,
)


@pytest.mark.parametrize(
    "text, expected",
    [
        ("hr:0", ModbusAddress("hr", 0)),
        ("holding:100", ModbusAddress("hr", 100)),
        ("holding 4", ModbusAddress("hr", 4)),
        ("ir:7", ModbusAddress("ir", 7)),
        ("input 0", ModbusAddress("ir", 0)),
        ("coil 3", ModbusAddress("co", 3)),
        ("di:2", ModbusAddress("di", 2)),
        ("hr:20.3", ModbusAddress("hr", 20, 3)),
        ("40001", ModbusAddress("hr", 0)),
        ("40101.15", ModbusAddress("hr", 100, 15)),
        ("30010", ModbusAddress("ir", 9)),
        ("10001", ModbusAddress("di", 0)),
        ("400001", ModbusAddress("hr", 0)),
        ("1", ModbusAddress("co", 0)),
    ],
)
def test_parse_address(text, expected):
    assert parse_address(text) == expected


@pytest.mark.parametrize("text", ["xx:1", "hr:-1", "hr:70000", "co:1.2", "hr:1.16", "50001", ""])
def test_parse_address_invalid(text):
    with pytest.raises(ValueError):
        parse_address(text)


@pytest.mark.parametrize("byte_order", ["big", "little"])
@pytest.mark.parametrize("word_order", ["big", "little"])
@pytest.mark.parametrize(
    "datatype, value",
    [
        ("int16", -1234),
        ("uint16", 65000),
        ("int32", -70000),
        ("uint32", 4_000_000_000),
        ("float32", 3.5),
        ("int64", -(2**40)),
        ("uint64", 2**63),
        ("float64", math.pi),
    ],
)
def test_roundtrip(datatype, value, byte_order, word_order):
    regs = encode_value(value, datatype, byte_order, word_order)
    assert decode_registers(regs, datatype, byte_order, word_order) == value


def test_known_layouts():
    # 1.0 as float32 is 0x3F800000
    assert encode_value(1.0, "float32") == [0x3F80, 0x0000]
    assert encode_value(1.0, "float32", word_order="little") == [0x0000, 0x3F80]
    assert encode_value(1.0, "float32", byte_order="little") == [0x803F, 0x0000]


def test_string_and_bits():
    regs = encode_value("ABC", "string", length=4)
    assert regs == [0x4142, 0x4300]
    assert decode_registers(regs, "string") == "ABC"
    assert decode_registers([0b1000], "bool", bit=3) is True
    assert decode_registers([0b1000], "bool", bit=2) is False


def _plan(address, count=1):
    return _TagPlan(
        TagConfig(name=address.replace(":", "_"), address=address),
        parse_address(address),
        "uint16",
        count,
    )


def test_plan_blocks_merges_nearby_registers():
    plans = [_plan("hr:0", 2), _plan("hr:5"), _plan("hr:40"), _plan("co:0"), _plan("co:9")]
    blocks = plan_blocks(plans, max_registers=100, max_gap=10)
    summary = [(b.area, b.start, b.count, len(b.tags)) for b in blocks]
    assert summary == [("co", 0, 10, 2), ("hr", 0, 6, 2), ("hr", 40, 1, 1)]


def test_plan_blocks_respects_max_size():
    plans = [_plan(f"hr:{i}") for i in range(0, 30)]
    blocks = plan_blocks(plans, max_registers=10, max_gap=0)
    assert [b.count for b in blocks] == [10, 10, 10]
