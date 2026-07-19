from datetime import datetime, timedelta, timezone
from typing import Any, Dict

import iso8601
import pytest
from aw_datastore import Datastore
from aw_core.models import Event
from aw_datastore.storages import MemoryStorage
from aw_query import query
from aw_query.exceptions import (
    QueryFunctionException,
    QueryInterpretException,
    QueryParseException,
)
from aw_query.query2 import (
    QDict,
    QFunction,
    QInteger,
    QList,
    QNumber,
    QString,
    QVariable,
    _parse_token,
    _split_query_statements,
)

from .utils import param_datastore_objects


class MockDatastore(MemoryStorage):
    pass


mock_ds = MockDatastore(testing=True)


def test_query2_test_token_parsing():
    ns: Dict[str, Any] = {}
    (t, token), trash = _parse_token("123", ns)
    assert token == "123"
    assert t == QNumber
    assert QInteger is QNumber
    (t, token), trash = _parse_token("-1.5e2", ns)
    assert token == "-1.5e2"
    assert t == QNumber
    (t, token), trash = _parse_token('"test"', ns)
    assert token == '"test"'
    assert t == QString
    (t, token), trash = _parse_token("'test'", ns)
    assert token == "'test'"
    assert t == QString
    (t, token), trash = _parse_token("'te\\'st'", ns)
    assert token == "'te\\'st'"
    assert t == QString
    (t, token), trash = _parse_token('"te\\"st"', ns)
    assert token == '"te\\"st"'
    assert t == QString
    (t, token), trash = _parse_token("test0xDEADBEEF", ns)
    assert token == "test0xDEADBEEF"
    assert t == QVariable
    (t, token), trash = _parse_token("test1337(')')", ns)
    assert token == "test1337(')')"
    assert t == QFunction
    (t, token), trash = _parse_token("test1337('test\\'test',\"test\\\"test\")", ns)
    assert token == "test1337('test\\'test',\"test\\\"test\")"
    assert t == QFunction
    (t, token), trash = _parse_token("[1, 'a', {}]", ns)
    assert token == "[1, 'a', {}]"
    assert t == QList
    (t, token), trash = _parse_token("{'a': 1, 'b}': 2}", ns)
    assert token == "{'a': 1, 'b}': 2}"
    assert t == QDict

    assert _parse_token("", ns) == ((None, ""), "")

    with pytest.raises(QueryParseException):
        _parse_token(None, ns)  # type: ignore

    with pytest.raises(QueryParseException):
        _parse_token('"', ns)

    with pytest.raises(QueryParseException):
        _parse_token("#", ns)

    assert QString.parse(r'"\w+"', ns).interpret(mock_ds, ns) == r"\w+"
    assert QString.parse(r'"a\\\"b"', ns).interpret(mock_ds, ns) == 'a\\"b'


def test_dict():
    ds = mock_ds
    ns: Dict[str, Any] = {}
    d_str = "{'a': {'a': {'a': 1}}, 'b': {'b\\'\"': ':'}}"
    d = QDict.parse(d_str, ns)
    expected_res = {"a": {"a": {"a": 1}}, "b": {"b'\"": ":"}}
    assert expected_res == d.interpret(ds, ns)

    # Key in dict is not a string
    with pytest.raises(QueryParseException):
        d_str = "{b: 1}"
        d = QDict.parse(d_str, ns)

    # Key in dict without a value
    with pytest.raises(QueryParseException):
        d_str = "{'test': }"
        d = QDict.parse(d_str, ns)

    # Char following key string is not a :
    with pytest.raises(QueryParseException):
        d_str = "{'test'p 1}"
        d = QDict.parse(d_str, ns)

    with pytest.raises(QueryParseException):
        d_str = "{'test': #}"
        d = QDict.parse(d_str, ns)

    # Semicolon without key
    with pytest.raises(QueryParseException):
        d_str = "{:}"
        d = QDict.parse(d_str, ns)

    # Trailing comma
    with pytest.raises(QueryParseException):
        d_str = "{'test':1,}"
        d = QDict.parse(d_str, ns)


def test_list():
    ds = mock_ds
    ns: Dict[str, Any] = {}
    l_str = "[1,2,[[3],4],5]"
    ls = QList.parse(l_str, ns)
    expected_res = [1, 2, [[3], 4], 5]
    assert expected_res == ls.interpret(ds, ns)

    l_str = "['\\'',\"\\\"\"]"
    ls = QList.parse(l_str, ns)
    expected_res = ["'", '"']
    assert expected_res == ls.interpret(ds, ns)

    l_str = "[]"
    ls = QList.parse(l_str, ns)
    expected_res = []
    assert expected_res == ls.interpret(ds, ns)

    # Comma without pre/post value
    with pytest.raises(QueryParseException):
        l_str = "[,]"
        ls = QList.parse(l_str, ns)

    # Comma without post value
    with pytest.raises(QueryParseException):
        l_str = "[1,]"
        ls = QList.parse(l_str, ns)

    # Comma without pre value
    with pytest.raises(QueryParseException):
        l_str = "[,2]"
        ls = QList.parse(l_str, ns)


def test_query2_bogus_query():
    ds = mock_ds
    qname = "test"
    qstartdate = datetime.now(tz=timezone.utc)
    qenddate = qstartdate

    # Nothing to assign
    with pytest.raises(QueryParseException):
        example_query = "a="
        query(qname, example_query, qstartdate, qenddate, ds)

    # Assign to non-variable
    with pytest.raises(QueryParseException):
        example_query = "1 = 2"
        query(qname, example_query, qstartdate, qenddate, ds)

    # Unclosed function
    with pytest.raises(QueryParseException):
        example_query = "a = unclosed_function(var1"
        query(qname, example_query, qstartdate, qenddate, ds)

    # Two tokens in assignment
    with pytest.raises(QueryParseException):
        example_query = "asd nop() = 2"
        query(qname, example_query, qstartdate, qenddate, ds)

    # Unclosed string
    with pytest.raises(QueryParseException):
        example_query = 'asd = "something is wrong with me'
        query(qname, example_query, qstartdate, qenddate, ds)

    # Two tokens in value
    with pytest.raises(QueryParseException):
        example_query = "asd = asd1 asd2"
        query(qname, example_query, qstartdate, qenddate, ds)


def test_query2_query_function_calling():
    ds = mock_ds
    qname = "asd"
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")

    # Function which doesn't exist
    with pytest.raises(QueryInterpretException):
        example_query = "RETURN = asd();"
        query(qname, example_query, starttime, endtime, ds)

    # Function which does exist with invalid arguments
    with pytest.raises(QueryInterpretException):
        example_query = "RETURN = nop(badarg);"
        query(qname, example_query, starttime, endtime, ds)

    # Function which does exist with valid arguments
    example_query = "RETURN = nop();"
    query(qname, example_query, starttime, endtime, ds)


def test_query2_return_value():
    ds = mock_ds
    qname = "asd"
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = "RETURN = 1;"
    result = query(qname, example_query, starttime, endtime, ds)
    assert result == 1

    example_query = "RETURN = -1.5e2;"
    result = query(qname, example_query, starttime, endtime, ds)
    assert result == -150.0

    example_query = "RETURN = 'testing 123'"
    result = query(qname, example_query, starttime, endtime, ds)
    assert result == "testing 123"

    example_query = "RETURN = {'a': 1}"
    result = query(qname, example_query, starttime, endtime, ds)
    assert result == {"a": 1}

    # Nothing to return
    with pytest.raises(QueryParseException):
        example_query = "a=1"
        result = query(qname, example_query, starttime, endtime, ds)


def test_query2_semicolon_in_string():
    """Test that semicolons inside string literals don't break query parsing (issue #145)."""
    ds = mock_ds
    qname = "asd"
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")

    # Semicolon in double-quoted string
    example_query = 'RETURN = "hello;world"'
    result = query(qname, example_query, starttime, endtime, ds)
    assert result == "hello;world"

    # Semicolon in single-quoted string
    example_query = "RETURN = 'hello;world'"
    result = query(qname, example_query, starttime, endtime, ds)
    assert result == "hello;world"

    # Multiple statements where one value contains a semicolon in a string
    example_query = 'a = "foo;bar"; RETURN = a'
    result = query(qname, example_query, starttime, endtime, ds)
    assert result == "foo;bar"


def test_split_query_statements():
    """Unit test for _split_query_statements."""
    # Basic split
    assert _split_query_statements("a=1;b=2") == ["a=1", "b=2"]
    # Semicolon inside double quotes should NOT split
    assert _split_query_statements('a="x;y";b=2') == ['a="x;y"', "b=2"]
    # Semicolon inside single quotes should NOT split
    assert _split_query_statements("a='x;y';b=2") == ["a='x;y'", "b=2"]
    # Trailing semicolon — empty trailing part is omitted
    assert _split_query_statements("a=1;") == ["a=1"]


def test_query2_multiline():
    ds = mock_ds
    qname = "asd"
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = """
my_multiline_string = "a
b";
RETURN = my_multiline_string;
    """
    result = query(qname, example_query, starttime, endtime, ds)
    assert result == "a\nb"


def test_query2_function_invalid_types():
    """Tests the q2_typecheck decorator"""
    ds = mock_ds
    qname = "asd"
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")

    # int instead of str
    example_query = """
        events = [];
        RETURN = filter_keyvals(events, 666, ["invalid_val"]);
    """
    with pytest.raises(QueryFunctionException):
        query(qname, example_query, starttime, endtime, ds)

    # str instead of list
    example_query = """
        events = [];
        RETURN = filter_keyvals(events, "2", "invalid_val");
    """
    with pytest.raises(QueryFunctionException):
        query(qname, example_query, starttime, endtime, ds)

    # FIXME: For unknown reasons, query2 drops the second argument
    #        when the first argument is a bare []
    """
    example_query = '''
        RETURN = filter_keyvals([], "2", "invalid_val");
    '''
    with pytest.raises(QueryFunctionException) as e:
        result = query(qname, example_query, starttime, endtime, None)
    """


def test_query2_function_invalid_argument_count():
    ds = mock_ds
    qname = "asd"
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = "RETURN=nop(nop())"
    with pytest.raises(QueryInterpretException):
        query(qname, example_query, starttime, endtime, ds)


def test_query2_merge_subwatcher_fields_invalid_conflict():
    ds = mock_ds
    qname = "asd"
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = """
        base = [];
        sub = [];
        RETURN = merge_subwatcher_fields(base, sub, ["project"], "invalid");
    """

    with pytest.raises(QueryFunctionException, match="conflict must be"):
        query(qname, example_query, starttime, endtime, ds)


@pytest.mark.parametrize("datastore", param_datastore_objects())
def test_query2_function_in_function(datastore):
    qname = "asd"
    bid = "test_bucket"
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = f"""
    RETURN=limit_events(query_bucket("{bid}"), 1);
    """
    try:
        # Setup buckets
        bucket1 = datastore.create_bucket(
            bucket_id=bid, type="test", client="test", hostname="test", name="test"
        )

        # Prepare buckets
        e1 = Event(data={}, timestamp=starttime, duration=timedelta(seconds=1))
        bucket1.insert(e1)
        result = query(qname, example_query, starttime, endtime, datastore)
        assert 1 == len(result)
    finally:
        datastore.delete_bucket(bid)


def test_query_bucket_optional_keeps_query_bucket_strict():
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")

    assert (
        query(
            "optional-missing-bucket",
            'RETURN = query_bucket_optional("missing");',
            starttime,
            endtime,
            mock_ds,
        )
        == []
    )
    with pytest.raises(QueryFunctionException, match="no bucket named"):
        query(
            "strict-missing-bucket",
            'RETURN = query_bucket("missing");',
            starttime,
            endtime,
            mock_ds,
        )


def test_query_bucket_optional_rejects_wrong_hostname():
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    bucket_id = "optional-hostname"
    datastore = Datastore(storage_strategy=MemoryStorage, testing=True)
    bucket = datastore.create_bucket(
        bucket_id=bucket_id,
        type="test",
        client="test",
        hostname="host-b",
        name="test",
    )
    bucket.insert(Event(timestamp=starttime, duration=timedelta(seconds=1), data={}))
    try:
        assert query(
            "optional-correct-hostname",
            f'RETURN = query_bucket_optional("{bucket_id}", "host-b");',
            starttime,
            endtime,
            datastore,
        )
        assert (
            query(
                "optional-wrong-hostname",
                f'RETURN = query_bucket_optional("{bucket_id}", "host-a");',
                starttime,
                endtime,
                datastore,
            )
            == []
        )
    finally:
        datastore.delete_bucket(bucket_id)


def test_query2_merge_subwatcher_fields_options_variable():
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = """
    base = [];
    context = [];
    fields = ["vdesktop"];
    options = {"source_id": "vdesktop", "conflict": "base_wins"};
    RETURN = merge_subwatcher_fields(base, context, fields, options);
    """

    assert query("options", example_query, starttime, endtime, mock_ds) == []


def test_query2_merge_subwatcher_fields_rejects_duplicate_source_id():
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = """
    RETURN = merge_subwatcher_fields(
        [],
        [],
        [],
        {"source_id": "options"},
        "positional"
    );
    """

    with pytest.raises(QueryFunctionException, match="not both"):
        query("duplicate-source-id", example_query, starttime, endtime, mock_ds)


def test_query2_merge_subwatcher_fields_rejects_non_string_source_id():
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = """
    RETURN = merge_subwatcher_fields([], [], [], {"source_id": 42});
    """

    with pytest.raises(QueryFunctionException, match="source_id must be a string"):
        query("invalid-source-type", example_query, starttime, endtime, mock_ds)


def test_query2_function_accepts_inline_dictionary_after_list():
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = """
    base = [];
    context = [];
    RETURN = merge_subwatcher_fields(
        base,
        context,
        ["vdesktop"],
        {"source_id": "vdesktop", "conflict": "base_wins"}
    );
    """

    assert query("inline-options", example_query, starttime, endtime, mock_ds) == []


def test_query2_function_preserves_commas_inside_nested_arguments():
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = """
    RETURN = nop(
        ["first", {"nested": ["a", "b"]}],
        {"first": "one", "second": "two"}
    );
    """

    with pytest.raises(QueryInterpretException, match="invalid amount of arguments"):
        query("nested-arguments", example_query, starttime, endtime, mock_ds)


def test_query2_strings_can_end_with_an_even_number_of_backslashes():
    starttime = iso8601.parse_date("1970-01-01")
    endtime = iso8601.parse_date("1970-01-02")
    example_query = r"""
    value = {"regex": "path\\", "quoted": "a\"b"};
    RETURN = value;
    """

    assert query("backslashes", example_query, starttime, endtime, mock_ds) == {
        "regex": r"path\\",
        "quoted": 'a"b',
    }


@pytest.mark.parametrize("datastore", param_datastore_objects())
def test_query2_query_functions(datastore):
    """
    Just test calling all functions just to see something isn't completely broken
    In many cases the functions doesn't change the result at all, so it's not a test
    for testing the validity of the data the functions transform
    """
    bid = "test_'bucket"
    qname = "test"
    starttime = iso8601.parse_date("1970")
    endtime = starttime + timedelta(hours=1)

    example_query = """
    bid = "{bid}";
    events = query_bucket("{bid}");
    events2 = query_bucket('{bid_escaped}');
    events2 = filter_keyvals(events2, "label", ["test1"]);
    events2 = exclude_keyvals(events2, "label", ["test2"]);
    events = filter_period_intersect(events, events2);
    events = filter_keyvals_regex(events, "label", ".*");
    events = limit_events(events, 1);
    events = merge_events_by_keys(events, ["label"]);
    events = chunk_events_by_key(events, "label");
    events = split_url_events(events);
    events = sort_by_timestamp(events);
    events = sort_by_duration(events);
    events = categorize(events, [[["test", "subtest"], {{"regex": "test1"}}]]);
    duration = sum_durations(events);
    eventcount = query_bucket_eventcount(bid);
    asd = nop();
    RETURN = {{"events": events, "eventcount": eventcount}};
    """.format(bid=bid, bid_escaped=bid.replace("'", "\\'"))
    try:
        bucket = datastore.create_bucket(
            bucket_id=bid, type="test", client="test", hostname="test", name="asd"
        )
        bucket.insert(
            Event(
                data={"label": "test1"},
                timestamp=starttime,
                duration=timedelta(seconds=1),
            )
        )
        result = query(qname, example_query, starttime, endtime, datastore)
        assert result["eventcount"] == 1
        assert len(result["events"]) == 1
        assert result["events"][0].data["label"] == "test1"
        assert result["events"][0].data["$category"] == ["test", "subtest"]
    finally:
        datastore.delete_bucket(bid)


@pytest.mark.parametrize("datastore", param_datastore_objects())
def test_query2_basic_query(datastore):
    name = "A label/name for a test bucket"
    bid1 = "bucket1"
    bid2 = "bucket2"
    qname = "test_query_basic"
    starttime = iso8601.parse_date("1970")
    endtime = starttime + timedelta(hours=1)

    example_query = f"""
    bid1 = "{bid1}";
    bid2 = "{bid2}";
    events = query_bucket(bid1);
    intersect_events = query_bucket(bid2);
    RETURN = filter_period_intersect(events, intersect_events);
    """

    try:
        # Setup buckets
        bucket1 = datastore.create_bucket(
            bucket_id=bid1, type="test", client="test", hostname="test", name=name
        )
        bucket2 = datastore.create_bucket(
            bucket_id=bid2, type="test", client="test", hostname="test", name=name
        )
        # Prepare buckets
        e1 = Event(
            data={"label": "test1"}, timestamp=starttime, duration=timedelta(seconds=1)
        )
        e2 = Event(
            data={"label": "test2"},
            timestamp=starttime + timedelta(seconds=2),
            duration=timedelta(seconds=1),
        )
        et = Event(
            data={"label": "intersect-label"},
            timestamp=starttime,
            duration=timedelta(seconds=1),
        )
        bucket1.insert(e1)
        bucket1.insert(e2)
        bucket2.insert(et)
        # Query
        result = query(qname, example_query, starttime, endtime, datastore)
        # Assert
        assert len(result) == 1
        assert result[0]["data"]["label"] == "test1"
    finally:
        datastore.delete_bucket(bid1)
        datastore.delete_bucket(bid2)


@pytest.mark.parametrize("datastore", param_datastore_objects())
def test_query2_test_merged_keys(datastore):
    name = "A label/name for a test bucket"
    bid = "bucket1"
    qname = "test_query_merged_keys"
    starttime = iso8601.parse_date("2080")
    endtime = starttime + timedelta(hours=1)

    example_query = f"""
    bid1 = "{bid}";
    events = query_bucket(bid1);
    events = merge_events_by_keys(events, ["label1", "label2"]);
    events = sort_by_duration(events);
    eventcount = query_bucket_eventcount(bid1);
    RETURN = {{"events": events, "eventcount": eventcount}};
    """
    try:
        # Setup buckets
        bucket1 = datastore.create_bucket(
            bucket_id=bid, type="test", client="test", hostname="test", name=name
        )
        # Prepare buckets
        e1 = Event(
            data={"label1": "test1", "label2": "test1"},
            timestamp=starttime,
            duration=timedelta(seconds=1),
        )
        e2 = Event(
            data={"label1": "test1", "label2": "test1"},
            timestamp=starttime + timedelta(seconds=1),
            duration=timedelta(seconds=1),
        )
        e3 = Event(
            data={"label1": "test1", "label2": "test2"},
            timestamp=starttime + timedelta(seconds=2),
            duration=timedelta(seconds=1),
        )
        bucket1.insert(e3)
        bucket1.insert(e1)
        bucket1.insert(e2)
        # Query
        result = query(qname, example_query, starttime, endtime, datastore)
        # Assert
        print(result)
        assert len(result["events"]) == 2
        assert result["eventcount"] == 3
        assert result["events"][0]["data"]["label1"] == "test1"
        assert result["events"][0]["data"]["label2"] == "test1"
        assert result["events"][0]["duration"] == timedelta(seconds=2)
        assert result["events"][1]["data"]["label1"] == "test1"
        assert result["events"][1]["data"]["label2"] == "test2"
        assert result["events"][1]["duration"] == timedelta(seconds=1)
    finally:
        datastore.delete_bucket(bid)


@pytest.mark.parametrize("datastore", param_datastore_objects())
def test_query2_fancy_query(datastore):
    """
    Tests:
     - find_bucket
     - simplify_window_titles
    """
    name = "A label/name for a test bucket"
    bid1 = "bucket-the-one"
    qname = "test_query_basic_fancy"
    starttime = iso8601.parse_date("1970")
    endtime = starttime + timedelta(hours=1)

    example_query = f"""
    bid = find_bucket("{bid1[:10]}");
    events = query_bucket(bid);
    RETURN = simplify_window_titles(events, "title");
    """

    try:
        # Setup buckets
        bucket_main = datastore.create_bucket(
            bucket_id=bid1, type="test", client="test", hostname="test", name=name
        )
        # Prepare buckets
        e1 = Event(
            data={"title": "(2) YouTube"},
            timestamp=starttime,
            duration=timedelta(seconds=1),
        )
        bucket_main.insert(e1)
        # Query
        result = query(qname, example_query, starttime, endtime, datastore)
        # Assert
        assert result[0]["data"]["title"] == "YouTube"
    finally:
        datastore.delete_bucket(bid1)


@pytest.mark.parametrize("datastore", param_datastore_objects())
def test_query2_query_categorize(datastore):
    bid = "test_bucket"
    qname = "test"
    starttime = iso8601.parse_date("1970")
    endtime = starttime + timedelta(hours=1)

    example_query = rf"""
    events = query_bucket("{bid}");
    events = sort_by_timestamp(events);
    events = categorize(events, [
                [["test"], {{"regex": "test"}}],
                [["test", "subtest"], {{"regex": "test\w"}}]
            ]);
    events_by_cat = merge_events_by_keys(events, ["$category"]);
    RETURN = {{"events": events, "events_by_cat": events_by_cat}};
    """
    try:
        bucket = datastore.create_bucket(
            bucket_id=bid, type="test", client="test", hostname="test", name="asd"
        )
        events = [
            Event(
                data={"label": "test"},
                timestamp=starttime,
                duration=timedelta(seconds=1),
            ),
            Event(
                data={"label": "testwithmoredetail"},
                timestamp=starttime + timedelta(seconds=1),
                duration=timedelta(seconds=1),
            ),
            Event(
                data={"label": "testwithmoredetail"},
                timestamp=starttime + timedelta(seconds=2),
                duration=timedelta(seconds=1),
            ),
        ]
        bucket.insert(events)
        result = query(qname, example_query, starttime, endtime, datastore)
        print(result)
        assert len(result["events"]) == 3
        assert result["events"][0].data["label"] == "test"
        assert result["events"][0].data["$category"] == ["test"]
        assert result["events"][1].data["$category"] == ["test", "subtest"]

        assert len(result["events_by_cat"]) == 2
        assert result["events_by_cat"][0].data["$category"] == ["test"]
        assert result["events_by_cat"][1].data["$category"] == ["test", "subtest"]
        assert result["events_by_cat"][1].duration == timedelta(seconds=2)
    finally:
        datastore.delete_bucket(bid)


@pytest.mark.parametrize("datastore", param_datastore_objects())
def test_query2_query_categorize_v2(datastore):
    bid = "test_bucket_v2"
    context_bid = "test_bucket_v2_context"
    qname = "test"
    starttime = iso8601.parse_date("1970")
    endtime = starttime + timedelta(hours=1)
    example_query = rf"""
    events = query_bucket("{bid}");
    context = query_bucket("{context_bid}");
    events = merge_subwatcher_fields(events, context, ["vdesktop"], {{"source_id": "vdesktop"}});
    events = map_event_fields(events, {{"title": "app"}});
    category_specs = [
        {{
            "id": "work",
            "name": ["Work"],
            "rule": {{"type": "regex", "host": "test", "field": "app", "regex": "devenv"}}
        }},
        {{
            "id": "personal",
            "name": ["Personal"],
            "rule": {{
                "type": "all",
                "rules": [
                    {{"type": "regex", "field": "app", "regex": "devenv"}},
                    {{"type": "regex", "source": "vdesktop", "field": "vdesktop", "regex": "Personal", "weight": 10}}
                ]
            }}
        }}
    ];
    events = categorize_v2(events, category_specs, "test");
    events = categorize_v2_explain(events, category_specs, "test");
    RETURN = events;
    """
    try:
        bucket = datastore.create_bucket(
            bucket_id=bid, type="test", client="test", hostname="test", name="test"
        )
        context_bucket = datastore.create_bucket(
            bucket_id=context_bid,
            type="test.context",
            client="test",
            hostname="test",
            name="context",
        )
        bucket.insert(
            Event(
                data={"app": "devenv.exe"},
                timestamp=starttime,
                duration=timedelta(seconds=1),
            )
        )
        context_bucket.insert(
            Event(
                data={"vdesktop": "Personal"},
                timestamp=starttime,
                duration=timedelta(seconds=1),
            )
        )
        result = query(qname, example_query, starttime, endtime, datastore)
        assert result[0].data["$category"] == ["Personal"]
        assert result[0].data["$category_score"] == 10
        assert result[0].data["$category_explain"]["winner"] == "personal"
    finally:
        datastore.delete_bucket(bid)
        datastore.delete_bucket(context_bid)


@pytest.mark.parametrize("datastore", param_datastore_objects()[1:])
def test_query2_active_periods_v2(datastore):
    window_bid = "test_active_periods_window"
    afk_bid = "test_active_periods_afk"
    starttime = iso8601.parse_date("1970")
    endtime = starttime + timedelta(hours=1)
    code = rf"""
    window = query_bucket("{window_bid}");
    afk = query_bucket("{afk_bid}");
    active_rule = {{
        "type": "all",
        "rules": [
            {{"type": "regex", "source": "window", "field": "app", "regex": "Teams"}},
            {{"type": "regex", "source": "afk", "host": "test", "field": "status", "regex": "not-afk"}}
        ]
    }};
    active_sources = [["window", window], ["afk", afk]];
    RETURN = active_periods_v2(active_sources, active_rule, "test");
    """
    try:
        window = datastore.create_bucket(
            bucket_id=window_bid,
            type="test.window",
            client="test",
            hostname="test",
            name="window",
        )
        afk = datastore.create_bucket(
            bucket_id=afk_bid,
            type="test.afk",
            client="test",
            hostname="test",
            name="afk",
        )
        window.insert(
            Event(
                timestamp=starttime,
                duration=timedelta(minutes=10),
                data={"app": "Teams"},
            )
        )
        afk.insert(
            Event(
                timestamp=starttime + timedelta(minutes=2),
                duration=timedelta(minutes=3),
                data={"status": "not-afk"},
            )
        )

        result = query("active-periods", code, starttime, endtime, datastore)
        assert len(result) == 1
        assert result[0].timestamp == starttime + timedelta(minutes=2)
        assert result[0].duration == timedelta(minutes=3)
    finally:
        if window_bid in datastore.buckets():
            datastore.delete_bucket(window_bid)
        if afk_bid in datastore.buckets():
            datastore.delete_bucket(afk_bid)


@pytest.mark.parametrize("datastore", param_datastore_objects()[1:])
def test_query2_signed_integer_and_null_literals(datastore):
    starttime = iso8601.parse_date("1970")
    result = query(
        "literals",
        'RETURN = {"negative": -1, "nothing": null};',
        starttime,
        starttime + timedelta(seconds=1),
        datastore,
    )

    assert result == {"negative": -1, "nothing": None}
