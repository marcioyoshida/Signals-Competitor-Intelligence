"""#292: static Onça secrets moved from Secrets Manager to SSM SecureString."""
import subprocess

from src import secretvalue


class _Fake:
    def __init__(self):
        self.calls = []

    def get_parameter(self, Name, WithDecryption=False):
        self.calls.append(("ssm", Name, WithDecryption))
        return {"Parameter": {"Value": "v"}}

    def get_secret_value(self, SecretId):
        self.calls.append(("sm", SecretId))
        return {"SecretString": "s"}


def test_slash_name_reads_ssm_securestring_decrypted():
    f = _Fake()
    assert secretvalue.read("/onca/api-key", client=f) == "v"
    assert f.calls == [("ssm", "/onca/api-key", True)]


def test_other_name_reads_secrets_manager():
    f = _Fake()
    assert secretvalue.read("signalscompetitor/onca/google-oauth", client=f) == "s"
    assert f.calls == [("sm", "signalscompetitor/onca/google-oauth")]


MOVED = ("signalscompetitor/onca/api-key", "signalscompetitor/onca/directory-reviewer",
         "signalscompetitor/onca/qa-test-credentials")


def test_no_code_reads_the_moved_secret_manager_names():
    """The Secrets Manager copies are deleted after the move; a reader left behind would
    silently lose its key ("not configured"), so none may remain in code or infra."""
    out = subprocess.run(["git", "grep", "-n", "-e", MOVED[0], "-e", MOVED[1], "-e", MOVED[2],
                          "--", "src", "infra/*.py", "qa_pipeline/*.py", "qa_pipeline/**/*.py",
                          "scripts/*.py", "scripts/**/*.py", "tests/e2e_app"],
                         capture_output=True, text=True)
    hits = [ln for ln in out.stdout.splitlines() if "was Secrets Manager" not in ln]
    assert hits == [], hits
