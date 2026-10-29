#!/usr/bin/env python3
"""Standalone project downloader generated for projects using model catalogs."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath


LEGACY_LOCAL_CONFIG_RELATIVE = Path(".data") / "model_artifact_manager" / "config.json"
PROVIDERS = {"huggingface", "github_release", "pypi_package"}


class DownloadError(Exception):
    """Expected configuration, provider, or file safety error."""


def _project_tool_directory(root):
    tools_path = root / "tools_py"
    if tools_path.is_dir():
        if tools_path.is_symlink() or not tools_path.resolve().is_relative_to(root.resolve()):
            raise DownloadError(f"Project tools directory must stay inside the project: {tools_path}")
        matches = sorted(
            path.parent
            for path in tools_path.glob("*/catalog.json")
            if path.is_file()
        )
        if len(matches) == 1:
            match = matches[0]
            catalog_path = match / "catalog.json"
            if (
                match.is_symlink()
                or catalog_path.is_symlink()
                or not match.resolve().is_relative_to(root.resolve())
                or not catalog_path.resolve().is_relative_to(root.resolve())
            ):
                raise DownloadError(f"Project downloader directory must stay inside the project: {match}")
            return match.relative_to(root)
        if len(matches) > 1:
            raise DownloadError(
                f"Expected one project model catalog under {tools_path}, found {len(matches)}."
            )
    raise DownloadError(f"No project model catalog was found under {tools_path}")


def _relative(value, label, allow_dot=False):
    if allow_dot and value == ".":
        return PurePosixPath(".")
    if not isinstance(value, str) or not value.strip():
        raise DownloadError(f"{label} must be a non-empty relative path")
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        path.is_absolute()
        or PureWindowsPath(value).is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
        or "\x00" in value
        or any(
            part.endswith((".", " "))
            or any(character in '<>:"|?*' or ord(character) < 32 for character in part)
            for part in path.parts
        )
    ):
        raise DownloadError(f"{label} must stay within its configured directory: {value!r}")
    return path


def _read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise DownloadError(f"Cannot read configuration {path}: {error}") from error
    if not isinstance(value, dict):
        raise DownloadError(f"Expected a JSON object in {path}")
    return value


def _ensure_local_config(root, tool_directory):
    config = root / ".data" / tool_directory.name / "config.json"
    if config.is_file():
        return config, False
    legacy_config = root / LEGACY_LOCAL_CONFIG_RELATIVE
    example = root / tool_directory / "config.example.json"
    if not example.is_file():
        raise DownloadError(f"Local configuration and neutral example are missing: {config}, {example}")
    config.parent.mkdir(parents=True, exist_ok=True)
    source = legacy_config if legacy_config.is_file() else example
    try:
        with source.open("rb") as source_file, config.open("xb") as target:
            shutil.copyfileobj(source_file, target)
    except FileExistsError:
        pass
    if source == legacy_config and config.is_file() and source.read_bytes() == config.read_bytes():
        source.unlink()
    return config, "created" if source == example else "migrated"


def _load_project(root, initialize_local_config=True):
    tool_directory = _project_tool_directory(root)
    catalog_path = root / tool_directory / "catalog.json"
    if not catalog_path.is_file():
        raise DownloadError(f"Model catalog not found: {catalog_path}")
    config_path = root / ".data" / tool_directory.name / "config.json"
    if initialize_local_config:
        config_path, created = _ensure_local_config(root, tool_directory)
        if created == "created":
            print(
                f"Created neutral local configuration at {config_path}; "
                "using its default models_root. Edit it to choose another output directory."
            )
        elif created == "migrated":
            print(f"Migrated the existing local model path to {config_path}.")
    catalog = _read_json(catalog_path)
    config = _read_json(config_path) if config_path.is_file() else None
    if catalog.get("schema_version") != 1:
        raise DownloadError("Catalog schema_version must be 1")
    if config is not None and config.get("schema_version") != 1:
        raise DownloadError("Local config schema_version must be 1")
    project_id = catalog.get("project_id")
    if not isinstance(project_id, str) or not project_id.strip():
        raise DownloadError("Catalog project_id must be a non-empty string")
    models_root = None
    if config is not None:
        models_root_value = config.get("models_root")
        if not isinstance(models_root_value, str) or not models_root_value.strip():
            raise DownloadError("Local config models_root must be a non-empty path")
        models_root = Path(os.path.expandvars(models_root_value)).expanduser()
        if not models_root.is_absolute():
            models_root = root / models_root
    artifacts = catalog.get("artifacts")
    if not isinstance(artifacts, list):
        raise DownloadError("Catalog artifacts must be an array")
    ids = set()
    file_ids = set()
    for index, artifact in enumerate(artifacts):
        field = f"artifacts[{index}]"
        if not isinstance(artifact, dict):
            raise DownloadError(f"{field} must be an object")
        artifact_id = artifact.get("id")
        if (
            not isinstance(artifact_id, str)
            or not artifact_id
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for character in artifact_id)
        ):
            raise DownloadError(f"{field}.id contains unsupported characters")
        if artifact_id in ids:
            raise DownloadError(f"Duplicate artifact id: {artifact_id}")
        ids.add(artifact_id)
        _relative(artifact.get("destination"), f"{field}.destination", allow_dot=True)
        source = artifact.get("source")
        if (
            not isinstance(source, dict)
            or not isinstance(source.get("provider"), str)
            or source["provider"] not in PROVIDERS
        ):
            raise DownloadError(f"{field}.source.provider must be one of {sorted(PROVIDERS)}")
        _validate_source(source, field)
        entries = source["assets"] if source["provider"] == "github_release" else source["files"]
        for file_entry in entries:
            file_id = file_entry.get("id")
            if file_id is not None:
                if file_id in file_ids:
                    raise DownloadError(f"Duplicate model file id: {file_id}")
                file_ids.add(file_id)
    destinations = []
    for artifact in artifacts:
        destination = _relative(artifact["destination"], "artifact.destination", allow_dot=True)
        parts = tuple(part.casefold() for part in destination.parts)
        if any(
            parts[:len(existing)] == existing
            or existing[:len(parts)] == parts
            for existing in destinations
        ):
            raise DownloadError(f"Artifact destinations overlap: {destination}")
        destinations.append(parts)
    return catalog, models_root.resolve() if models_root is not None else None


def _validate_source(source, field):
    provider = source["provider"]
    if provider == "huggingface":
        if not isinstance(source.get("repo_id"), str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
            source["repo_id"],
        ):
            raise DownloadError(f"{field}.source.repo_id must use owner/name format")
        if not isinstance(source.get("revision"), str) or not source["revision"].strip():
            raise DownloadError(f"{field}.source.revision must be pinned explicitly")
        entries = source.get("files")
        key = "path"
    elif provider == "github_release":
        if not isinstance(source.get("repository"), str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*",
            source["repository"],
        ):
            raise DownloadError(f"{field}.source.repository must use owner/name format")
        if not isinstance(source.get("tag"), str) or not source["tag"].strip():
            raise DownloadError(f"{field}.source.tag must be pinned explicitly")
        entries = source.get("assets")
        key = "asset"
    else:
        package = source.get("package")
        version = source.get("version")
        if not isinstance(package, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", package):
            raise DownloadError(f"{field}.source.package must be set")
        if not isinstance(version, str) or not re.fullmatch(
            r"\d+(?:\.\d+)*(?:(?:a|b|rc)\d+)?(?:\.post\d+)?(?:\.dev\d+)?(?:\+[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*)?",
            version,
        ):
            raise DownloadError(f"{field}.source.version must be an exact version")
        entries = source.get("files")
        key = "path"
    if not isinstance(entries, list) or not entries:
        raise DownloadError(f"{field}.source must explicitly select one or more files")
    destinations = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise DownloadError(f"{field}.source file entry {index} must be an object")
        _relative(entry.get(key), f"{field}.source file entry {index}.{key}")
        target = _relative(
            entry.get("destination", PurePosixPath(entry[key]).name),
            f"{field}.source file entry {index}.destination",
        )
        if target.as_posix().casefold() == "artifact-manifest.json":
            raise DownloadError("artifact-manifest.json is reserved for download provenance")
        target_parts = tuple(part.casefold() for part in target.parts)
        if any(
            target_parts[:len(existing)] == existing
            or existing[:len(target_parts)] == target_parts
            for existing in destinations
        ):
            raise DownloadError(f"Overlapping output paths in {field}: {target}")
        destinations.add(target_parts)
        expected = entry.get("sha256")
        if expected is not None and (
            not isinstance(expected, str)
            or len(expected) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in expected)
        ):
            raise DownloadError(f"{field}.source file entry {index}.sha256 must be 64 hex characters")
        file_id = entry.get("id")
        if file_id is not None and (
            not isinstance(file_id, str)
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", file_id)
        ):
            raise DownloadError(f"{field}.source file entry {index}.id is invalid")
        if "default" in entry and not isinstance(entry["default"], bool):
            raise DownloadError(f"{field}.source file entry {index}.default must be a boolean")
        if "default" in entry and file_id is None:
            raise DownloadError(f"{field}.source file entry {index} needs an id when default is specified")


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path, expected, label):
    actual = _sha256(path)
    if expected and actual.casefold() != expected.casefold():
        raise DownloadError(f"SHA-256 mismatch for {label}: expected {expected}, received {actual}")
    return actual


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        redirected = super().redirect_request(request, response, code, message, headers, new_url)
        if redirected is not None and urllib.parse.urlsplit(request.full_url).hostname != urllib.parse.urlsplit(new_url).hostname:
            redirected.remove_header("Authorization")
        return redirected


def _request(url, output, headers=None):
    opener = urllib.request.build_opener(_SafeRedirect)
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "model-artifact-manager/1", **(headers or {})},
    )
    try:
        with opener.open(request, timeout=90) as response, output.open("wb") as target:
            shutil.copyfileobj(response, target, length=1024 * 1024)
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as error:
        raise DownloadError(f"Download failed from {url}: {error}") from error


def _download_huggingface(source, entries, stage):
    owner, repo = source["repo_id"].split("/", 1)
    token = os.environ.get("HF_TOKEN")
    downloaded = []
    for entry in entries:
        remote = _relative(entry["path"], "Hugging Face path").as_posix()
        target = _relative(entry.get("destination", PurePosixPath(remote).name), "Hugging Face output path")
        revision = urllib.parse.quote(source["revision"], safe="")
        quoted_repo = urllib.parse.quote(f"{owner}/{repo}", safe="/")
        quoted_path = urllib.parse.quote(remote, safe="/")
        url = f"https://huggingface.co/{quoted_repo}/resolve/{revision}/{quoted_path}"
        staged = stage.joinpath(*target.parts)
        staged.parent.mkdir(parents=True, exist_ok=True)
        headers = {"Authorization": f"Bearer {token}"} if token else None
        _request(url, staged, headers)
        downloaded.append((entry, target, staged, f"huggingface:{source['repo_id']}@{source['revision']}:{remote}"))
    return downloaded


def _download_github(source, entries, stage):
    owner, repo = source["repository"].split("/", 1)
    tag = urllib.parse.quote(source["tag"], safe="")
    metadata_path = stage / ".release-metadata.json"
    _request(f"https://api.github.com/repos/{owner}/{repo}/releases/tags/{tag}", metadata_path,
             {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})
    release = _read_json(metadata_path)
    metadata_path.unlink()
    if release.get("tag_name") != source["tag"] or not isinstance(release.get("assets"), list):
        raise DownloadError(f"GitHub did not return the requested release {source['repository']}@{source['tag']}")
    output = []
    for entry in entries:
        matches = [asset for asset in release["assets"] if asset.get("name") == entry["asset"]]
        if len(matches) != 1:
            raise DownloadError(f"Expected one release asset {entry['asset']!r}; found {len(matches)}")
        url = matches[0].get("browser_download_url")
        prefix = f"https://github.com/{owner}/{repo}/releases/download/"
        if not isinstance(url, str) or not url.startswith(prefix):
            raise DownloadError(f"Unexpected URL for GitHub asset {entry['asset']!r}")
        target = _relative(entry.get("destination", entry["asset"]), "GitHub output path")
        staged = stage.joinpath(*target.parts)
        staged.parent.mkdir(parents=True, exist_ok=True)
        _request(url, staged)
        output.append((entry, target, staged, f"github_release:{source['repository']}@{source['tag']}:{entry['asset']}"))
    return output


def _download_pypi(source, entries, stage):
    wheel_dir = stage / ".wheel"
    payload_dir = stage / ".payload"
    wheel_dir.mkdir()
    payload_dir.mkdir()
    command = [
        sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
        "--disable-pip-version-check", "--dest", str(wheel_dir),
        f"{source['package']}=={source['version']}",
    ]
    try:
        subprocess.run(
            command, check=True, capture_output=True, text=True, timeout=600,
            env={**os.environ, "PIP_NO_INPUT": "1"},
        )
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise DownloadError(f"Could not download {source['package']}=={source['version']}: {detail}") from error
    except (OSError, subprocess.TimeoutExpired) as error:
        raise DownloadError(f"Could not download {source['package']}=={source['version']}: {error}") from error
    wheels = list(wheel_dir.glob("*.whl"))
    if len(wheels) != 1:
        raise DownloadError(f"Expected one compatible wheel; found {len(wheels)}")
    wheel_hash = _sha256(wheels[0])
    output = []
    try:
        with zipfile.ZipFile(wheels[0]) as archive:
            names = set(archive.namelist())
            for entry in entries:
                member = _relative(entry["path"], "PyPI wheel member").as_posix()
                if member not in names:
                    raise DownloadError(f"Selected wheel member not found: {member}")
                target = _relative(entry.get("destination", PurePosixPath(member).name), "PyPI output path")
                staged = payload_dir.joinpath(*target.parts)
                staged.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(member) as member_stream, staged.open("wb") as destination:
                    shutil.copyfileobj(member_stream, destination, length=1024 * 1024)
                output.append((
                    entry, target, staged,
                    f"pypi:{source['package']}=={source['version']}:{member}",
                    wheels[0].name, wheel_hash,
                ))
    except (OSError, zipfile.BadZipFile, RuntimeError) as error:
        raise DownloadError(f"Could not inspect PyPI wheel {wheels[0].name}: {error}") from error
    return output

def _download_artifact(artifact, models_root, overwrite):
    artifact_id = artifact["id"]
    destination = _relative(artifact["destination"], f"{artifact_id}.destination", allow_dot=True)
    final_directory = models_root.joinpath(*destination.parts)
    if not final_directory.resolve().is_relative_to(models_root.resolve()):
        raise DownloadError(f"Artifact output escapes models_root: {final_directory}")
    source = artifact["source"]
    manifest_source = artifact.get("_manifest_source", source)
    stage_parent = models_root
    stage_parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{artifact_id}-stage-", dir=stage_parent) as temp:
        stage = Path(temp)
        if source["provider"] == "huggingface":
            files = _download_huggingface(source, source["files"], stage)
        elif source["provider"] == "github_release":
            files = _download_github(source, source["assets"], stage)
        else:
            files = _download_pypi(source, source["files"], stage)
        enriched = []
        targets = []
        for row in files:
            entry, relative, staged, source_label = row[:4]
            wheel_name, wheel_digest = row[4:] if len(row) > 4 else (None, None)
            digest = _verify(staged, entry.get("sha256"), source_label)
            target = final_directory.joinpath(*relative.parts)
            if not target.resolve().is_relative_to(final_directory.resolve()):
                raise DownloadError(f"Selected output escapes artifact directory: {target}")
            targets.append(target)
            enriched.append({
                "source": source_label,
                "destination": relative.as_posix(),
                "staged": staged,
                "sha256": digest,
                "size_bytes": staged.stat().st_size,
                "wheel": wheel_name,
                "wheel_sha256": wheel_digest,
            })
        manifest_path = final_directory / "artifact-manifest.json"
        existing = [path for path in targets if path.exists()]
        previous_manifest = None
        if manifest_path.is_file():
            try:
                previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise DownloadError(f"Could not read existing artifact manifest {manifest_path}: {error}") from error
            if (
                not isinstance(previous_manifest, dict)
                or previous_manifest.get("schema_version") != 1
                or previous_manifest.get("artifact_id") != artifact_id
                or not isinstance(previous_manifest.get("files"), list)
            ):
                raise DownloadError(
                    f"Existing artifact manifest is invalid or belongs to another artifact: {manifest_path}"
                )
            if any(not isinstance(record, dict) or not isinstance(record.get("path"), str) for record in previous_manifest["files"]):
                raise DownloadError(f"Existing artifact manifest has invalid file records: {manifest_path}")
        if existing and not overwrite:
            raise DownloadError(
                "Refusing to overwrite existing files: "
                + ", ".join(str(path) for path in existing)
                + "; pass --overwrite after review."
            )
        final_directory.mkdir(parents=True, exist_ok=True)
        records = []
        for item, target in zip(enriched, targets):
            target.parent.mkdir(parents=True, exist_ok=True)
            temp_output = None
            try:
                with tempfile.NamedTemporaryFile(
                    prefix=".model-download-", suffix=".tmp", dir=target.parent, delete=False
                ) as output:
                    temp_output = Path(output.name)
                    with item["staged"].open("rb") as staged:
                        shutil.copyfileobj(staged, output, length=1024 * 1024)
                os.replace(temp_output, target)
            finally:
                if temp_output is not None:
                    temp_output.unlink(missing_ok=True)
            record = {
                "source": item["source"],
                "path": (destination / PurePosixPath(item["destination"])).as_posix(),
                "sha256": item["sha256"],
                "size_bytes": item["size_bytes"],
            }
            if item["wheel"]:
                record["package_wheel"] = item["wheel"]
                record["package_wheel_sha256"] = item["wheel_sha256"]
            records.append(record)
        download_manifest = {
            "schema_version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "artifact_id": artifact_id,
            "provider": source["provider"],
            "source": manifest_source,
            "files": records,
        }
        if previous_manifest is not None:
            previous_files = previous_manifest["files"]
            merged_files = {record["path"]: record for record in previous_files}
            merged_files.update({record["path"]: record for record in download_manifest["files"]})
            manifest = {
                **previous_manifest,
                "updated_at": download_manifest["created_at"],
                "provider": source["provider"],
                "source": manifest_source,
                "files": [merged_files[path] for path in sorted(merged_files)],
            }
        else:
            manifest = download_manifest
        temp_manifest = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", prefix=".manifest-", suffix=".tmp",
                dir=final_directory, delete=False,
            ) as output:
                temp_manifest = Path(output.name)
                json.dump(manifest, output, indent=2, ensure_ascii=False)
                output.write("\n")
            os.replace(temp_manifest, manifest_path)
        finally:
            if temp_manifest is not None:
                temp_manifest.unlink(missing_ok=True)
    return download_manifest


def _run_converter(project_root, extra_args):
    catalog_path = project_root / _project_tool_directory(project_root) / "catalog.json"
    catalog = _read_json(catalog_path)
    converter = catalog.get("converter")
    if not isinstance(converter, dict):
        raise DownloadError(f"No project converter is configured in {catalog_path}")
    command = converter.get("command")
    if not isinstance(command, list) or not command or not all(
        isinstance(part, str) and part.strip() for part in command
    ):
        raise DownloadError("converter.command must be a non-empty array of strings")
    working_directory = converter.get("working_directory", ".")
    if working_directory == ".":
        cwd = project_root
    else:
        cwd = project_root.joinpath(*_relative(working_directory, "converter.working_directory").parts)
    cwd = cwd.resolve()
    if not cwd.is_relative_to(project_root) or not cwd.is_dir():
        raise DownloadError(f"Converter working directory must exist inside the project: {cwd}")
    try:
        return subprocess.run(
            [*command, *extra_args],
            cwd=cwd,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
            check=False,
        ).returncode
    except OSError as error:
        raise DownloadError(f"Could not start converter command {command[0]!r}: {error}") from error


def _select_artifact_files(
    artifact,
    selected_file_ids=None,
    include_all=False,
    include_model_cards=False,
):
    source = artifact["source"]
    key = "assets" if source["provider"] == "github_release" else "files"
    entries = source[key]
    if selected_file_ids is not None:
        selected_entries = [entry for entry in entries if entry.get("id") in selected_file_ids]
    elif not include_all and any("default" in entry for entry in entries):
        selected_entries = [
            entry for entry in entries
            if entry.get("default") is True
            or (include_model_cards and entry.get("kind") == "model_card")
        ]
    else:
        selected_entries = entries
    if not selected_entries:
        return None
    selected_artifact = dict(artifact)
    selected_source = dict(source)
    selected_source[key] = selected_entries
    selected_artifact["source"] = selected_source
    selected_artifact["_manifest_source"] = source
    return selected_artifact


def main(argv=None):
    parser = argparse.ArgumentParser(description="Download explicitly selected project model artifacts.")
    parser.add_argument("--project-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--list", action="store_true", help="List configured models and their provenance metadata")
    parser.add_argument(
        "--publisher-type",
        choices=("official", "community"),
        help="Limit model selection to the selected publisher class",
    )
    parser.add_argument("--artifact", action="append", default=[], help="Artifact id; repeat to select multiple")
    parser.add_argument(
        "--file",
        action="append",
        default=None,
        help="Exact catalog file id; repeat to select files instead of catalog defaults",
    )
    parser.add_argument(
        "--include-model-cards",
        action="store_true",
        help="Add catalog files marked kind=model_card to the default file selection",
    )
    parser.add_argument("--overwrite", action="store_true", help="Replace selected local files after review")
    parser.add_argument("--convert", action="store_true", help="Run the explicitly configured project converter")
    parser.add_argument("converter_args", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    try:
        project_root = args.project_root.expanduser().resolve()
        if not project_root.is_dir():
            raise DownloadError(f"Project root does not exist: {project_root}")
        if args.list and args.convert:
            raise DownloadError("--list cannot be combined with --convert")
        if args.convert:
            converter_args = args.converter_args
            if converter_args and converter_args[0] == "--":
                converter_args = converter_args[1:]
            return _run_converter(project_root, converter_args)
        catalog, models_root = _load_project(
            project_root,
            initialize_local_config=not args.list,
        )
        if args.list:
            if args.artifact or args.file is not None or args.overwrite or args.include_model_cards:
                raise DownloadError(
                    "--list cannot be combined with artifact, file, overwrite, "
                    "or include-model-cards options"
                )
            listed_artifacts = catalog["artifacts"]
            if args.publisher_type:
                listed_artifacts = [
                    item for item in listed_artifacts
                    if item.get("metadata", {}).get("publisher_type") == args.publisher_type
                ]
                if not listed_artifacts:
                    raise DownloadError(f"No artifacts match publisher type {args.publisher_type!r}")
            result = []
            for artifact in listed_artifacts:
                source = artifact["source"]
                result.append({
                    "id": artifact["id"],
                    "metadata": artifact.get("metadata", {}),
                    "provider": source["provider"],
                    "repository": source.get("repo_id", source.get("repository")),
                    "revision": source.get("revision", source.get("tag", source.get("version"))),
                    "files": [
                        {
                            "id": entry.get("id"),
                            "kind": entry.get("kind"),
                            "path": entry.get("path", entry.get("asset")),
                            "default": entry.get("default", False),
                        }
                        for entry in source.get("assets", source.get("files", []))
                    ],
                })
            print(json.dumps(result, indent=2, ensure_ascii=False))
            return 0
        if args.include_model_cards and args.file is not None:
            raise DownloadError("--include-model-cards cannot be combined with --file")
        available_artifacts = catalog["artifacts"]
        if args.publisher_type:
            available_artifacts = [
                item for item in available_artifacts
                if item.get("metadata", {}).get("publisher_type") == args.publisher_type
            ]
            if not available_artifacts:
                raise DownloadError(f"No artifacts match publisher type {args.publisher_type!r}")
        selected = set(args.artifact)
        known = {item["id"] for item in available_artifacts}
        unknown = selected - known
        if unknown:
            if args.publisher_type:
                raise DownloadError(
                    f"Unknown artifact id(s) for publisher type {args.publisher_type!r}: "
                    f"{', '.join(sorted(unknown))}"
                )
            raise DownloadError(f"Unknown artifact id(s): {', '.join(sorted(unknown))}")
        artifacts = [item for item in available_artifacts if not selected or item["id"] in selected]
        if not artifacts:
            raise DownloadError("No model artifacts are configured in the project catalog")
        requested_files = set(args.file) if args.file is not None else None
        available_files = {
            entry["id"]
            for artifact in artifacts
            for entry in (
                artifact["source"]["assets"]
                if artifact["source"]["provider"] == "github_release"
                else artifact["source"]["files"]
            )
            if "id" in entry
        }
        unknown_files = sorted((requested_files or set()) - available_files)
        if unknown_files:
            raise DownloadError(f"Unknown or unselected model file id(s): {', '.join(unknown_files)}")
        artifacts = [
            filtered
            for artifact in artifacts
            if (
                filtered := _select_artifact_files(
                    artifact,
                    requested_files,
                    include_all=bool(selected) and requested_files is None,
                    include_model_cards=args.include_model_cards,
                )
            ) is not None
        ]
        if not artifacts:
            if requested_files is None:
                raise DownloadError("No default model files are configured; select files with --file.")
            raise DownloadError("No model artifacts contain the selected files")
        for artifact in artifacts:
            manifest = _download_artifact(artifact, models_root, args.overwrite)
            print(f"Downloaded {artifact['id']}: {len(manifest['files'])} file(s)")
            for record in manifest["files"]:
                print(f"  {models_root / record['path']}  sha256={record['sha256']}")
        return 0
    except (DownloadError, OSError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
