import os


def test_files_exist():
    files = [
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        ".github/workflows/ci.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/pull_request_template.md",
    ]
    for file in files:
        assert os.path.isfile(file), f"{file} does not exist"


def test_contributing_mentions_verify():
    with open("CONTRIBUTING.md", "r") as f:
        content = f.read()
    assert "scripts/verify.sh" in content, "CONTRIBUTING.md mentions scripts/verify.sh"


def test_security_mentions_advisories():
    with open("SECURITY.md", "r") as f:
        content = f.read()
    assert "security advisories" in content.lower(), "SECURITY.md mentions security advisories"


def test_no_email_or_hostname():
    files_with_email_check = [
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/pull_request_template.md",
    ]
    files_with_hostname_check = [
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
        ".github/workflows/ci.yml",
        ".github/ISSUE_TEMPLATE/bug_report.yml",
        ".github/ISSUE_TEMPLATE/feature_request.yml",
        ".github/pull_request_template.md",
    ]
    for file in files_with_email_check:
        with open(file, "r") as f:
            content = f.read()
        assert "@" not in content, f"{file} contains an email address"
    for file in files_with_hostname_check:
        with open(file, "r") as f:
            content = f.read()
        assert "http" not in content, f"{file} contains a hostname"