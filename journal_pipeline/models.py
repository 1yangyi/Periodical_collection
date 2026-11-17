from dataclasses import dataclass


@dataclass(frozen=True)
class Journal:
    id: str
    name: str
    urls: list[str]
    bucket: str


@dataclass
class Resource:
    url: str
    content: bytes
    headers: dict[str, str]
    text: str = ''
    rendered: bool = False
    protocol_fallback: dict | None = None
