import pytest

from benchmarks.prompt_retrieval_eval import validate_database_url


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+asyncpg://local:synthetic@production.example.test/prompt_opt",
        "postgresql+asyncpg://local:synthetic@127.0.0.1/vpnsupport",
        "postgresql+asyncpg://local:synthetic@127.0.0.1/postgres",
        "postgresql://local:synthetic@127.0.0.1/prompt_opt",
    ],
)
def test_retrieval_probe_rejects_non_disposable_database(url: str) -> None:
    with pytest.raises(ValueError, match="disposable loopback"):
        validate_database_url(url)


def test_retrieval_probe_accepts_explicit_ephemeral_database() -> None:
    url = "postgresql+asyncpg://local:synthetic@127.0.0.1:55493/prompt_opt"
    assert validate_database_url(url) == url
