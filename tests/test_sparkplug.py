import math
from types import SimpleNamespace

import pytest

from tagbridge.sparkplug import DataType, Metric, Payload, decode_payload, encode_payload, topic

CASES = [
    (DataType.INT8, -5),
    (DataType.INT16, -32768),
    (DataType.INT32, -2_000_000_000),
    (DataType.INT64, -(2**60)),
    (DataType.UINT8, 255),
    (DataType.UINT16, 65535),
    (DataType.UINT32, 4_000_000_000),
    (DataType.UINT64, 2**64 - 1),
    (DataType.FLOAT, 1.5),
    (DataType.DOUBLE, math.pi),
    (DataType.BOOLEAN, True),
    (DataType.BOOLEAN, False),
    (DataType.STRING, "héllo"),
    (DataType.DATETIME, 1_700_000_000_000),
]


@pytest.mark.parametrize("datatype, value", CASES)
def test_roundtrip(datatype, value):
    payload = Payload(timestamp=123, seq=7, metrics=[Metric("a/b", value, datatype, 9, 456)])
    decoded = decode_payload(encode_payload(payload))
    assert decoded.timestamp == 123
    assert decoded.seq == 7
    metric = decoded.metrics[0]
    assert (metric.name, metric.alias, metric.timestamp, metric.datatype) == (
        "a/b",
        9,
        456,
        datatype,
    )
    assert metric.value == value


def test_null_metric():
    data = encode_payload(Payload(metrics=[Metric("x", None, DataType.DOUBLE)]))
    metric = decode_payload(data).metrics[0]
    assert metric.is_null and metric.value is None


def test_topic():
    assert topic("G", "NBIRTH", "E") == "spBv1.0/G/NBIRTH/E"
    assert topic("G", "DDATA", "E", "D") == "spBv1.0/G/DDATA/E/D"


@pytest.mark.parametrize("datatype, value", CASES)
def test_interop_with_reference_protobuf(datatype, value):
    """Payloads must parse with the protobuf runtime using the sparkplug_b.proto field layout."""
    pb = reference_schema()
    payload = Payload(timestamp=1, seq=2, metrics=[Metric("m", value, datatype, 3, 4)])
    parsed = pb.Payload()
    parsed.ParseFromString(encode_payload(payload))
    metric = parsed.metrics[0]
    assert metric.name == "m" and metric.alias == 3 and metric.datatype == int(datatype)
    field = metric.WhichOneof("value")
    raw = getattr(metric, field)
    if datatype == DataType.FLOAT:
        assert raw == pytest.approx(value)
    elif field in ("int_value", "long_value"):
        bits = 32 if field == "int_value" else 64
        assert raw == value & ((1 << bits) - 1)
    else:
        assert raw == value
    # and the reverse direction
    assert decode_payload(parsed.SerializeToString()).metrics[0].value == pytest.approx(value)


def reference_schema():
    """Build Payload/Metric classes with the field numbers and types of sparkplug_b.proto."""
    pytest.importorskip("google.protobuf")
    from google.protobuf import descriptor_pb2, descriptor_pool, message_factory

    fd = descriptor_pb2.FieldDescriptorProto
    file = descriptor_pb2.FileDescriptorProto(
        name="spb_ref.proto", package="spbref", syntax="proto2"
    )
    payload = file.message_type.add(name="Payload")
    metric = payload.nested_type.add(name="Metric")
    metric.oneof_decl.add(name="value")
    for number, name, kind, oneof in [
        (1, "name", fd.TYPE_STRING, False),
        (2, "alias", fd.TYPE_UINT64, False),
        (3, "timestamp", fd.TYPE_UINT64, False),
        (4, "datatype", fd.TYPE_UINT32, False),
        (5, "is_historical", fd.TYPE_BOOL, False),
        (6, "is_transient", fd.TYPE_BOOL, False),
        (7, "is_null", fd.TYPE_BOOL, False),
        (10, "int_value", fd.TYPE_UINT32, True),
        (11, "long_value", fd.TYPE_UINT64, True),
        (12, "float_value", fd.TYPE_FLOAT, True),
        (13, "double_value", fd.TYPE_DOUBLE, True),
        (14, "boolean_value", fd.TYPE_BOOL, True),
        (15, "string_value", fd.TYPE_STRING, True),
        (16, "bytes_value", fd.TYPE_BYTES, True),
    ]:
        field = metric.field.add(name=name, number=number, type=kind, label=fd.LABEL_OPTIONAL)
        if oneof:
            field.oneof_index = 0
    for number, name, kind in [
        (1, "timestamp", fd.TYPE_UINT64),
        (3, "seq", fd.TYPE_UINT64),
        (4, "uuid", fd.TYPE_STRING),
        (5, "body", fd.TYPE_BYTES),
    ]:
        payload.field.add(name=name, number=number, type=kind, label=fd.LABEL_OPTIONAL)
    payload.field.add(
        name="metrics",
        number=2,
        type=fd.TYPE_MESSAGE,
        type_name=".spbref.Payload.Metric",
        label=fd.LABEL_REPEATED,
    )
    pool = descriptor_pool.DescriptorPool()
    pool.Add(file)
    cls = message_factory.GetMessageClass(pool.FindMessageTypeByName("spbref.Payload"))
    return SimpleNamespace(Payload=cls)
