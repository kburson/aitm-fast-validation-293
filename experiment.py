"""Execute one exact candidate in the disposable hosted comparison."""

import json
import os
import re
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from hashlib import sha1, sha256
from io import BytesIO
from pathlib import Path
from time import monotonic

LAB = Path(__file__).resolve().parent
PRODUCT = "packages/aitm-usage"
CLI = "tools/ci/tests/test_cli_evidence.py::test_usage_console_help"
PROFILES = [f"{PRODUCT}/tests", "tools/ci/tests"]
SCHEMA = "aitm.pytest-evidence/v1"
NAME = "kburson/aitm-fast-validation-293"


def read(path):
    return json.loads(path.read_bytes())


def dump(path, data):
    path.write_text(json.dumps(data, sort_keys=True, indent=2) + "\n")


def git(*args):
    return subprocess.check_output(["git", "-C", str(LAB), *args])


def line(*args):
    return git(*args).decode().strip()


def digest(value):
    return sha256(value).hexdigest()


def subject(event, catalog, manifest):
    data = event["pull_request"]
    repo = event["repository"]
    base, head, tested = (
        data["base"]["sha"],
        data["head"]["sha"],
        os.environ["GITHUB_SHA"],
    )
    require = re.compile(r"[0-9a-f]{40}").fullmatch
    assert all(map(require, (base, head, tested)))
    assert repo["full_name"] == NAME and data["base"]["ref"] == "trunk"
    assert repo["id"] == data["base"]["repo"]["id"] == data["head"]["repo"]["id"]
    assert os.environ["GITHUB_REF"] == f"refs/pull/{data['number']}/merge"
    assert line("rev-parse", "HEAD") == tested
    assert line("show", "-s", "--format=%P", tested).split() == [base, head]
    assert line("rev-parse", f"{base}:source") == catalog["source_tree"]
    assert git("show", f"{base}:experiment.py") == Path(__file__).read_bytes()
    assert read(LAB / "cases.json") == json.loads(git("show", f"{base}:cases.json"))
    assert manifest == json.loads(git("show", f"{base}:workload.json"))
    changes = git("diff", "--no-renames", "--raw", "-z", "--no-abbrev", base, tested)
    fields, path, end = changes.split(b"\0")
    assert not end and path.startswith(b"source/")
    old_mode, new_mode, before, after, status = fields.decode().split()
    assert new_mode == "100644" and (old_mode, status) in {
        (":000000", "A"),
        (":100644", "M"),
    }
    source_path = path.decode().removeprefix("source/")
    (case,) = [
        (name, spec)
        for name, spec in catalog["cases"].items()
        if spec["path"] == source_path and spec["after"] == after
    ]
    name, spec = case
    assert spec["before"] == (before if status == "M" else None)
    return (
        {
            "repository": NAME,
            "repository_id": repo["id"],
            "pull": data["number"],
            "base": base,
            "head": head,
            "tested": tested,
            "tree": line("rev-parse", f"{tested}^{{tree}}"),
            "source_tree": line("rev-parse", f"{tested}:source"),
            "source_baseline": catalog["source_sha"],
            "case": name,
            "source_path": source_path,
            "source_blob": after,
            "edit_frozen_at": line("show", "-s", "--format=%cI", head),
        },
        spec,
        changes.replace(b"\0source/", b"\0"),
    )


def verify_archive(root, tree):
    manifest = {}
    for row in git("ls-tree", "-rz", tree).removesuffix(b"\0").split(b"\0"):
        header, path = row.split(b"\t", 1)
        mode, kind, oid = header.split()
        assert kind == b"blob"
        manifest[path] = mode, oid
    actual = {}
    for f in root.rglob("*"):
        if f.is_symlink():
            data, mode = os.fsencode(os.readlink(f)), b"120000"
        elif f.is_dir():
            continue
        else:
            assert f.is_file()
            data, mode = (
                f.read_bytes(),
                b"100755" if f.stat().st_mode & 0o100 else b"100644",
            )
        blob = sha1(f"blob {len(data)}\0".encode() + data).hexdigest().encode()
        actual[os.fsencode(f.relative_to(root))] = mode, blob
    if actual != manifest:
        missing = sorted(manifest.keys() - actual.keys())
        extra = sorted(actual.keys() - manifest.keys())
        changed = sorted(
            name
            for name in actual.keys() & manifest.keys()
            if actual[name] != manifest[name]
        )
        raise ValueError(
            f"Tested archive drift. Missing {missing!r}, extra {extra!r}, changed {changed!r}"
        )


def collection(evidence):
    assert evidence["schema"] == SCHEMA and evidence["exit_code"] == 0
    assert not any(evidence[k] for k in ("reports", "children", "deselected"))
    nodes = evidence["collected"]
    assert all(isinstance(node, str) and "::" in node for node in nodes)
    assert len(set(nodes)) == len(nodes) > 0
    return nodes


def reports_by_case(raw, assigned, skip_policy):
    cases = defaultdict(dict)
    for row in raw:
        node, phase = row["nodeid"], row["when"]
        assert node in assigned and phase in ("setup", "call", "teardown")
        assert phase not in cases[node] and row["wasxfail"] is False
        cases[node][phase] = row
    assert set(cases) == assigned
    outcomes = {}
    for node, stages in cases.items():
        assert stages["teardown"]["outcome"] == "passed", (node, stages)
        setup, call = stages["setup"], stages.get("call")
        if (
            setup["outcome"] == "passed"
            and call
            and call["outcome"] in ("passed", "failed")
        ):
            outcomes[node] = call["outcome"]
        elif setup["outcome"] == "failed" and call is None:
            outcomes[node] = "setup-error"
        else:
            skipped = setup if call is None else call
            assert skipped["outcome"] == "skipped" and skip_policy.get(node) is not None
            assert skipped["skip_reason"] == skip_policy[node]
            outcomes[node] = "skipped"
    return dict(sorted(outcomes.items()))


def reconcile(directory, assigned, jobs, skip_policy, labels):
    dest = directory / "pytest.json"
    head = read(dest)
    assert (
        head["schema"] == SCHEMA
        and head["exit_code"] in (0, 1)
        and not head["deselected"]
    )
    if jobs == 1:
        assert head["collected"] == assigned
        reports = head["reports"]
        children = head["children"]
    else:
        workers = sorted(directory.glob("pytest-gw*.json"))
        assert [p.stem for p in workers] == [f"pytest-gw{i}" for i in range(jobs)]
        assert not head["collected"] and not head["children"]
        reports, children = [], []
        owners = {}
        for index, file in enumerate(workers):
            result = read(file)
            assert result["schema"] == SCHEMA and result["exit_code"] in (0, 1)
            assert result["collected"] == assigned and not result["deselected"]
            for row in result["reports"]:
                assert owners.setdefault(row["nodeid"], index) == index
            reports.extend(result["reports"])
            children.extend(result["children"])
        assert Counter(map(encode, reports)) == Counter(map(encode, head["reports"]))
    outcomes = reports_by_case(reports, set(assigned), skip_policy)
    assert head["exit_code"] == (
        1 if set(outcomes.values()) & {"failed", "setup-error"} else 0
    )
    assert Counter(c["label"] for c in children) == Counter(labels)
    assert all(
        type(c["pid"]) is int
        and c["pid"] > 0
        and type(c["returncode"]) is int
        and c["returncode"] == 0
        for c in children
    )
    assert len({c["pid"] for c in children}) == len(children)
    return {
        "assignment_count": len(assigned),
        "outcomes": outcomes,
        "children": children,
    }


def encode(obj):
    return json.dumps(obj, sort_keys=True)


def pytest(source, scratch, output, python, env, name, selected=None, jobs=1):
    where = output / name
    where.mkdir()
    evidence = where / "pytest.json"
    command = [
        str(python),
        "-m",
        "pytest",
        "-c",
        f"{PRODUCT}/pyproject.toml",
        "--rootdir",
        str(source),
        "-p",
        "no:cacheprovider",
        "-p",
        "pytest_evidence",
        "--basetemp",
        str(scratch / name),
        "-q",
        "--tb=long",
        "--durations=20",
    ]
    if jobs > 1:
        command.extend(["-p", "xdist.plugin", "-n", str(jobs), "--dist", "worksteal"])
    if selected is None:
        command.extend(["--collect-only", *PROFILES])
    else:
        assert selected
        args = where / "assigned.txt"
        args.write_text("\n".join(selected) + "\n")
        command.append(f"@{args}")
    start = monotonic()
    with (where / "pytest.log").open("wb") as log:
        result = subprocess.run(
            command,
            cwd=source,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**env, "AITM_CI_PYTEST_EVIDENCE": str(evidence)},
            timeout=1200,
        )
    print(
        name,
        "exit",
        result.returncode,
        "elapsed seconds",
        round(monotonic() - start, 2),
        flush=True,
    )
    assert (
        result.returncode in (0, 1) and read(evidence)["exit_code"] == result.returncode
    )
    return where, round(monotonic() - start, 3)


def run(lane, work, out):
    assert os.environ["GITHUB_EVENT_NAME"] == "pull_request"
    event = read(Path(os.environ["GITHUB_EVENT_PATH"]))
    catalog, manifest = read(LAB / "cases.json"), read(LAB / "workload.json")
    exact, case, raw = subject(event, catalog, manifest)
    out.mkdir()
    source, fixtures, home, temp = (
        work / d for d in ("source", "fixtures", "home", "temp")
    )
    for d in (source, fixtures, home, temp):
        d.mkdir()
    with tarfile.open(fileobj=BytesIO(git("archive", exact["source_tree"]))) as archive:
        archive.extractall(source, filter="data")
    verify_archive(source, exact["source_tree"])
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(source / "tools/ci"))
    from ci_fast_story_shadow import classify_diff

    edge = read(source / "tools/ci/tia/edges.json")
    witnessed_inputs = {
        d["input"]: d["consumer"] for d in edge["edges"] if d["kind"] == "fixture"
    }
    fixture_edge = (
        witnessed_inputs.get(exact["source_path"]) == catalog["fixture_consumer"]
    )
    fixture_class = fixture_edge and exact["source_path"] == catalog["fixture_path"]
    mode = "selected" if (classify_diff(raw) or fixture_class) else "full-required"
    assert mode == case["mode"]
    summary = {
        "schema": "aitm.qualification-293/v1",
        "scope": "disposable shadow",
        "subject": exact,
        "mode": mode,
        "lane": lane,
        "run": int(os.environ["GITHUB_RUN_ID"]),
        "attempt": int(os.environ["GITHUB_RUN_ATTEMPT"]),
        "cohorts": {},
        "model_sha256": digest((LAB / "workload.json").read_bytes()),
        "catalog_sha256": digest((LAB / "cases.json").read_bytes()),
    }
    dump(out / "start.json", summary)
    if lane == "fast" and mode == "full-required":
        dump(out / "result.json", summary)
        return
    env = {
        k: os.environ[k]
        for k in ("PATH", "LANG", "LC_ALL", "UV_PYTHON_INSTALL_DIR")
        if k in os.environ
    }
    env.update(
        HOME=str(home),
        TMPDIR=str(temp),
        PYTHONDONTWRITEBYTECODE="1",
        UV_PROJECT_ENVIRONMENT=str(work / "venv"),
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONPATH=f"{source / 'tools/ci'}:{source / PRODUCT / 'src'}",
        AITM_CI_CHILD_EVIDENCE="1",
    )
    with (out / "setup.log").open("wb") as log:
        subprocess.run(
            [
                "uv",
                "sync",
                "--locked",
                "--no-config",
                "--python",
                "3.13.9",
                "--project",
                str(source / PRODUCT),
            ],
            cwd=source,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
            check=True,
        )
    python = work / "venv/bin/python"
    data, _ = pytest(source, fixtures, out, python, env, "collection")
    inventory = collection(read(data / "pytest.json"))
    assert [n for n in inventory if n not in case["new_ids"]] == manifest["cohorts"][
        "full"
    ]
    assert set(inventory) - set(manifest["cohorts"]["full"]) == set(case["new_ids"])
    policy = read(source / "tools/ci/policy.json")
    mandatory = set(policy["required_tests"]) | {CLI}
    assert len(inventory) + len(manifest["cohorts"]["projected"]) >= 3020
    summary["inventory_size"] = len(inventory)
    for cohort in ("full", "projected"):
        rows = inventory if cohort == "full" else manifest["cohorts"][cohort]
        assert mandatory <= set(rows) <= set(inventory)
        selected = rows
        if lane == "fast":
            selected = [
                n
                for n in rows
                if n in mandatory
                or (
                    fixture_class
                    and n.partition("::")[0] == catalog["fixture_consumer"]
                )
            ]
        workers = 1 if lane == "fast" else 3
        run_dir, duration = pytest(
            source, fixtures, out, python, env, cohort, selected, workers
        )
        measured = reconcile(
            run_dir,
            selected,
            workers,
            policy["optional_skips"],
            policy["required_children"],
        )
        measured.update(seconds=duration, assigned=selected)
        summary["cohorts"][cohort] = measured
    verify_archive(source, exact["source_tree"])
    assert line("rev-parse", "HEAD") == exact["tested"]
    dump(out / "result.json", summary)


if __name__ == "__main__":
    branch, workdir, artifact = sys.argv[1:4]
    assert branch in ("fast", "full")
    run(branch, Path(workdir), Path(artifact))
