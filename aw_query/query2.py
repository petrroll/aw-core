import logging
import re
from abc import abstractmethod
from datetime import datetime
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Sequence,
    Tuple,
    Type,
)

from aw_datastore import Datastore

from .exceptions import QueryInterpretException, QueryParseException
from .functions import functions

logger = logging.getLogger(__name__)


class QToken:
    @abstractmethod
    def interpret(self, datastore: Datastore, namespace: dict):
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def parse(string: str, namespace: dict):
        raise NotImplementedError

    @staticmethod
    @abstractmethod
    def check(string: str) -> Tuple[Optional[str], str]:
        raise NotImplementedError


class QNumber(QToken):
    def __init__(self, value) -> None:
        self.value = value

    def interpret(self, datastore: Datastore, namespace: dict):
        return self.value

    @staticmethod
    def parse(string: str, namespace: dict) -> QToken:
        if "." in string or "e" in string.lower():
            return QNumber(float(string))
        return QNumber(int(string))

    @staticmethod
    def check(string: str):
        match = re.match(
            r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?",
            string,
        )
        token = match.group(0) if match else ""
        return token, string[len(token) :]


# Compatibility for callers that imported the old internal token name.
QInteger = QNumber


class QVariable(QToken):
    def __init__(self, name, value) -> None:
        self.name = name
        self.value = value

    def interpret(self, datastore: Datastore, namespace: dict):
        if self.name not in namespace:
            raise QueryInterpretException(
                f"Tried to reference variable '{self.name}' which is not defined"
            )
        namespace[self.name] = self.value
        return self.value

    @staticmethod
    def parse(string: str, namespace: dict) -> QToken:
        val = None
        if string in namespace:
            val = namespace[string]
        return QVariable(string, val)

    @staticmethod
    def check(string: str):
        token = ""
        for i, char in enumerate(string):
            if char.isalpha() or char == "_":
                token += char
            elif i != 0 and char.isdigit():
                token += char
            else:
                break
        return token, string[len(token) :]


class QString(QToken):
    def __init__(self, value):
        self.value = value

    def interpret(self, datastore: Datastore, namespace: dict):
        return self.value

    @staticmethod
    def parse(string: str, namespace: dict) -> "QString":
        quotes_type = string[0]
        value = string[1:-1]
        decoded = ""
        index = 0
        while index < len(value):
            if value[index] != "\\":
                decoded += value[index]
                index += 1
                continue
            end = index
            while end < len(value) and value[end] == "\\":
                end += 1
            count = end - index
            if end < len(value) and value[end] == quotes_type and count % 2 == 1:
                decoded += "\\" * ((count - 1) // 2) + quotes_type
                index = end + 1
            else:
                decoded += "\\" * count
                index = end
        return QString(decoded)

    @staticmethod
    def check(string: str):
        token = ""
        quotes_type = string[0]
        if quotes_type != '"' and quotes_type != "'":
            return token, string
        token += quotes_type
        backslashes = 0
        for char in string[1:]:
            token += char
            if char == quotes_type and backslashes % 2 == 0:
                break
            backslashes = backslashes + 1 if char == "\\" else 0
        if token[-1] != quotes_type or len(token) < 2:
            # Unclosed string?
            raise QueryParseException("Failed to parse string")
        return token, string[len(token) :]


class QFunction(QToken):
    def __init__(self, name, args):
        self.name = name
        self.args = args

    def interpret(self, datastore: Datastore, namespace: dict):
        if self.name not in functions:
            raise QueryInterpretException(
                f"Tried to call function '{self.name}' which doesn't exist"
            )
        call_args = [datastore, namespace]
        for arg in self.args:
            call_args.append(arg.interpret(datastore, namespace))
        # logger.debug("Arguments for functioncall to {} is {}".format(self.name, call_args))
        try:
            result = functions[self.name](*call_args)  # type: ignore
        except TypeError:
            raise QueryInterpretException(
                f"Tried to call function {self.name} with invalid amount of arguments"
            ) from None
        return result

    @staticmethod
    def parse(string: str, namespace: dict) -> QToken:
        arg_start = 0
        arg_end = len(string) - 1
        # Find opening bracket
        for char in string:
            if char == "(":
                break
            arg_start = arg_start + 1
        # Parse name
        name = string[:arg_start]
        # Parse arguments
        args = []
        args_str = string[arg_start + 1 : arg_end]
        while args_str:
            (arg_t, arg), args_str = _parse_token(args_str, namespace)
            args_str = args_str.lstrip()
            if args_str.startswith(","):
                args_str = args_str[1:]
            args.append(arg_t.parse(arg, namespace))
        return QFunction(name, args)

    @staticmethod
    def check(string: str):
        i = 0
        # Find opening bracket
        found = False
        for char in string:
            if char.isalpha() or char == "_":
                i = i + 1
            elif i != 0 and char.isdigit():
                i = i + 1
            elif char == "(":
                i = i + 1
                found = True
                break
            else:
                break
        if not found:
            return None, string
        to_consume = 1
        single_quote = False
        double_quote = False
        backslashes = 0
        for char in string[i:]:
            i = i + 1
            escaped = backslashes % 2 == 1
            if char == "'" and not escaped and not double_quote:
                single_quote = not single_quote
            elif char == '"' and not escaped and not single_quote:
                double_quote = not double_quote
            elif single_quote or double_quote:
                pass
            elif i != 0 and char.isdigit():
                pass
            elif char == "(":
                to_consume += 1
            elif char == ")":
                to_consume -= 1
            if to_consume == 0:
                break
            backslashes = backslashes + 1 if char == "\\" else 0
        if to_consume != 0:
            return None, string
        return string[:i], string[i + 1 :]


class QDict(QToken):
    def __init__(self, value: dict) -> None:
        self.value = value

    def interpret(self, datastore: Datastore, namespace: dict):
        expanded_dict = {}
        for key, value in self.value.items():
            expanded_dict[key] = value.interpret(datastore, namespace)
        return expanded_dict

    @staticmethod
    def parse(string: str, namespace: dict) -> QToken:
        entries_str = string[1:-1]
        d: Dict[str, QToken] = {}
        while len(entries_str) > 0:
            entries_str = entries_str.strip()
            if len(d) > 0 and entries_str[0] == ",":
                entries_str = entries_str[1:]
            # parse key
            (key_t, key_str), entries_str = _parse_token(entries_str, namespace)
            if key_t != QString:
                raise QueryParseException("Key in dict is not a str")
            key = QString.parse(key_str, {}).value
            entries_str = entries_str.strip()
            # Remove :
            if entries_str[0] != ":":
                raise QueryParseException("Key in dict is not followed by a :")
            entries_str = entries_str[1:]
            # parse val
            (val_t, val_str), entries_str = _parse_token(entries_str, namespace)
            if not val_t:
                raise QueryParseException("Dict expected a value, got nothing")
            val = val_t.parse(val_str, namespace)
            # set
            d[key] = val
        return QDict(d)

    @staticmethod
    def check(string: str):
        if string[0] != "{":
            return None, string
        # Find closing bracket
        i = 1
        to_consume = 1
        single_quote = False
        double_quote = False
        backslashes = 0
        for char in string[i:]:
            i += 1
            escaped = backslashes % 2 == 1
            if char == "'" and not escaped and not double_quote:
                single_quote = not single_quote
            elif char == '"' and not escaped and not single_quote:
                double_quote = not double_quote
            elif single_quote or double_quote:
                pass
            elif char == "}":
                to_consume = to_consume - 1
            elif char == "{":
                to_consume = to_consume + 1
            if to_consume == 0:
                break
            backslashes = backslashes + 1 if char == "\\" else 0
        return string[:i], string[i + 1 :]


class QList(QToken):
    def __init__(self, value: list) -> None:
        self.value = value

    def interpret(self, datastore: Datastore, namespace: dict):
        expanded_list = []
        for value in self.value:
            expanded_list.append(value.interpret(datastore, namespace))
        return expanded_list

    @staticmethod
    def parse(string: str, namespace: dict) -> QToken:
        entries_str = string[1:-1]
        ls: List[QToken] = []
        while len(entries_str) > 0:
            entries_str = entries_str.strip()
            if len(ls) > 0 and entries_str[0] == ",":
                entries_str = entries_str[1:]
            # parse
            (val_t, val_str), entries_str = _parse_token(entries_str, namespace)
            if not val_t:
                raise QueryParseException("List expected a value, got nothing")
            val = val_t.parse(val_str, namespace)
            # set
            ls.append(val)
        return QList(ls)

    @staticmethod
    def check(string: str):
        if string[0] != "[":
            return None, string
        # Find closing bracket
        i = 1
        to_consume = 1
        single_quote = False
        double_quote = False
        backslashes = 0
        for char in string[i:]:
            i += 1
            escaped = backslashes % 2 == 1
            if char == "'" and not escaped and not double_quote:
                single_quote = not single_quote
            elif char == '"' and not escaped and not single_quote:
                double_quote = not double_quote
            elif double_quote or single_quote:
                pass
            elif char == "]":
                to_consume = to_consume - 1
            elif char == "[":
                to_consume = to_consume + 1
            if to_consume == 0:
                break
            backslashes = backslashes + 1 if char == "\\" else 0
        return string[:i], string[i + 1 :]


qtypes: Sequence[Type[QToken]] = [QString, QNumber, QFunction, QDict, QList, QVariable]


def _parse_token(string: str, namespace: dict) -> Tuple[Tuple[Any, str], str]:
    # TODO: The whole parsing thing is shoddily written, needs a rewrite from ground-up
    if not isinstance(string, str):
        raise QueryParseException(
            "Reached unreachable, cannot parse something that isn't a string"
        )
    if len(string) == 0:
        return (None, ""), string
    string = string.strip()
    token = None
    t = None  # Declare so we can return it
    for t in qtypes:
        token, string = t.check(string)
        if token:
            break
    if not token:
        raise QueryParseException(f"Syntax error: {string}")
    return (t, token), string


def create_namespace() -> dict:
    namespace = {
        "True": True,
        "False": False,
        "true": True,
        "false": False,
        "None": None,
        "null": None,
    }
    return namespace


def parse(line, namespace):
    separator_i = line.find("=")
    var_str = line[:separator_i]
    val_str = line[separator_i + 1 :]
    if not val_str:
        # TODO: Proper message
        raise QueryParseException("Nothing to assign")
    (var_t, var), var_str = _parse_token(var_str, namespace)
    var_str = var_str.strip()
    if var_str:  # Didn't consume whole var string
        raise QueryParseException("Invalid syntax for assignment variable")
    if var_t is not QVariable:
        raise QueryParseException("Cannot assign to a non-variable")
    (val_t, val), var_str = _parse_token(val_str, namespace)
    if var_str:  # Didn't consume whole val string
        raise QueryParseException("Invalid syntax for value to assign")
    # Parse token
    var = var_t.parse(var, namespace)
    val = val_t.parse(val, namespace)
    return var, val


def interpret(var, val, namespace, datastore):
    namespace[var.name] = val.interpret(datastore, namespace)
    # logger.debug("Set {} to {}".format(var.name, namespace[var.name]))


def get_return(namespace):
    if "RETURN" not in namespace:
        raise QueryParseException(
            "Query doesn't assign the RETURN variable, nothing to respond"
        )
    return namespace["RETURN"]


def _split_query_statements(query: str) -> List[str]:
    """Split query into statements on semicolons, ignoring semicolons inside string literals."""
    statements = []
    current = ""
    in_single_quote = False
    in_double_quote = False
    backslashes = 0

    for char in query:
        escaped = backslashes % 2 == 1
        if char == "'" and not escaped and not in_double_quote:
            in_single_quote = not in_single_quote
            current += char
        elif char == '"' and not escaped and not in_single_quote:
            in_double_quote = not in_double_quote
            current += char
        elif char == ";" and not in_single_quote and not in_double_quote:
            statements.append(current)
            current = ""
        else:
            current += char
        backslashes = backslashes + 1 if char == "\\" else 0

    if current:
        statements.append(current)
    return statements


def query(
    name: str, query: str, starttime: datetime, endtime: datetime, datastore: Datastore
) -> Any:
    namespace = create_namespace()
    namespace["NAME"] = name
    namespace["STARTTIME"] = starttime.isoformat()
    namespace["ENDTIME"] = endtime.isoformat()

    query_stmts = _split_query_statements(query)
    for statement in query_stmts:
        statement = statement.strip()
        if statement:
            logger.debug("Parsing: " + statement)
            var, val = parse(statement, namespace)
            interpret(var, val, namespace, datastore)

    result = get_return(namespace)
    return result
