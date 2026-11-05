"""Small shared modelDescription.xml parser for FastDyn's FMU tooling."""
from __future__ import annotations

from dataclasses import dataclass
import re
import xml.etree.ElementTree as ET


@dataclass(frozen=True)
class FmuVariable:
    name: str
    value_reference: int | None
    scalar_type: str


@dataclass(frozen=True)
class FmuMetadata:
    fmi_version: str
    model_identifier: str | None
    instantiation_token: str | None
    variables: tuple[FmuVariable, ...]

    def value_references(self) -> dict[str, int]:
        return {item.name: item.value_reference for item in self.variables
                if item.value_reference is not None}


def parse_model_description(xml: bytes | str) -> FmuMetadata:
    root = ET.fromstring(xml)
    cosimulation = next((item for item in root
                         if item.tag.rsplit("}", 1)[-1] == "CoSimulation"), None)
    identifier = cosimulation.get("modelIdentifier") if cosimulation is not None else None
    if identifier is not None and not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", identifier):
        identifier = None
    variables = []
    scalar_tags = {"Float32", "Float64", "Int8", "UInt8", "Int16", "UInt16",
                   "Int32", "UInt32", "Int64", "UInt64", "Boolean", "String",
                   "Enumeration", "Binary", "Clock", "ScalarVariable"}
    for element in root.iter():
        scalar_type = element.tag.rsplit("}", 1)[-1]
        if scalar_type not in scalar_tags or "name" not in element.attrib:
            continue
        raw = element.attrib.get("valueReference")
        try:
            reference = int(raw) if raw is not None else None
        except ValueError:
            reference = None
        variables.append(FmuVariable(element.attrib["name"], reference, scalar_type))
    return FmuMetadata(
        fmi_version=root.get("fmiVersion", ""), model_identifier=identifier,
        instantiation_token=root.get("instantiationToken") or root.get("guid"),
        variables=tuple(variables),
    )
