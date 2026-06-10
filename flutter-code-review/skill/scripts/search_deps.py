#!/usr/bin/env python3
import os
import sys
import subprocess
import zipfile
import threading
import shutil
import argparse
import json
from urllib.parse import unquote
from concurrent.futures import ThreadPoolExecutor
import time


# Default directory exclusions for faster searching
DEFAULT_EXCLUDES = [
    ".git",
    "aion_shell",
    "build_shell",
    "bin",
    "test",
    "log",
    "tmp",
    "temp",
    "git/cache",
]


def search_directory_rg(query, path, extensions=None, binary=False):
    if not os.path.exists(path):
        return []

    # rg flags: -n (line num), --no-heading, --color=never
    flags = ["-n", "--no-heading", "--color=never"]

    # Add exclusions
    for x in DEFAULT_EXCLUDES:
        flags.append("-g")
        flags.append(f"!**/{x}/*")

    if binary:
        flags.append("--text")
        # Optimization: Even in binary mode, we should skip common media files to save time
        # while still allowing searching in .a, .framework, etc.
        media_exts = [
            "png",
            "jpg",
            "jpeg",
            "gif",
            "webp",
            "pdf",
            "mp3",
            "mp4",
            "wav",
            "ttf",
            "otf",
        ]
        for ext in media_exts:
            flags.append("-g")
            flags.append(f"!*.{ext}")

    # Add glob filters for extensions if provided
    # e.g. -g "*.dart" -g "*.yaml"
    if extensions and not binary:
        for ext in extensions:
            flags.append("-g")
            flags.append(f"*{ext}")

    try:
        cmd = ["rg"] + flags + [query, path]
        # print(f"DEBUG: Running {' '.join(cmd)}", file=sys.stderr)
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip().splitlines()
        elif result.returncode == 1:
            # rg ran successfully but found NO matches.
            # Do NOT fallback to grep.
            return []
    except FileNotFoundError:
        pass
    except Exception as e:
        # Unexpected error running rg, try grep
        pass

    try:
        # Fallback to grep ONLY if rg is not installed
        flags_grep = ["-r", "-n"]
        for x in DEFAULT_EXCLUDES:
            flags_grep.append(f"--exclude-dir={x}")

        if binary:
            flags_grep.append("-a")

        # grep doesn't support -g easily in the same way across versions (include/exclude),
        # generally we just grep everything if rg fails, or use --include
        if extensions and not binary:
            for ext in extensions:
                flags_grep.append(f"--include=*{ext}")

        cmd = ["grep"] + flags_grep + [query, path]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode == 0:
            return result.stdout.strip().splitlines()
    except Exception as e:
        print(f"Error searching {path}: {e}", file=sys.stderr)

    return []


def search_archive(query, file_path):
    matches = []
    query_bytes = query.encode("utf-8")  # Prepare byte-level query for speed

    try:
        if not zipfile.is_zipfile(file_path):
            return []

        with zipfile.ZipFile(file_path, "r") as z:
            for filename in z.namelist():
                if filename.endswith("/"):
                    continue

                # 1. Fast Check: Filename Match
                # This is extremely fast and often what we want (finding the class file)
                if query in filename:
                    matches.append(f"{file_path}!{filename}:0: [Filename Match]")

                # 2. Content Check (Optimized)
                try:
                    # Read raw bytes - fastest way, no decoding yet
                    with z.open(filename) as f:
                        content = f.read()

                    # Binary Search: Check if bytes exist.
                    # This works for both UTF-8 text source and modified-UTF-8 strings in .class files.
                    if query_bytes in content:
                        # Determine if we should try to show context (Source Code)
                        is_source = filename.endswith(
                            (
                                ".java",
                                ".kt",
                                ".xml",
                                ".properties",
                                ".gradle",
                                ".h",
                                ".m",
                                ".mm",
                                ".c",
                                ".cpp",
                                ".json",
                                ".txt",
                            )
                        )

                        if is_source:
                            try:
                                # Only decode if we know it's a source file AND it contains the query
                                text = content.decode("utf-8")
                                lines = text.splitlines()
                                for i, line in enumerate(lines):
                                    if query in line:
                                        matches.append(
                                            f"{file_path}!{filename}:{i + 1}:{line.strip()}"
                                        )
                            except UnicodeDecodeError:
                                # Fallback if source file has weird encoding
                                matches.append(
                                    f"{file_path}!{filename}:0: [Binary Match (Decode Failed)]"
                                )
                        else:
                            # For .class and others, just report binary match
                            matches.append(f"{file_path}!{filename}:0: [Binary Match]")

                except Exception:
                    continue
    except Exception:
        pass
    return matches


def search_gradle_cache(query):
    gradle_home = os.path.expanduser("~/.gradle/caches/modules-2/files-2.1")
    if not os.path.exists(gradle_home):
        return []

    print(f"Scanning Gradle cache at {gradle_home}...", file=sys.stderr)

    # Optimization: Use rg (ripgrep) to pre-filter jars if available.
    # This is 100x faster than iterating with python.
    archives_to_search = []

    try:
        # -l: files with matches
        # -a: binary/text (treat all as text)
        # -F: fixed string (no regex overhead)
        # -L: follow symlinks
        # --no-messages: suppress permission errors
        cmd = ["rg", "-l", "-a", "-F", "-L", "--no-messages", query, gradle_home]

        # Run rg
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode == 0:
            raw_files = result.stdout.strip().splitlines()
            # Filter for jars/aars
            archives_to_search = [
                f for f in raw_files if f.endswith(".jar") or f.endswith(".aar")
            ]
            print(
                f"rg pre-filter found {len(archives_to_search)} candidate archives.",
                file=sys.stderr,
            )

    except FileNotFoundError:
        print(
            "rg (ripgrep) not found, falling back to slow full-scan...", file=sys.stderr
        )
        # Fallback to slow walk
        for root, dirs, files in os.walk(gradle_home):
            for file in files:
                if file.endswith(".jar") or file.endswith(".aar"):
                    archives_to_search.append(os.path.join(root, file))

    if not archives_to_search and shutil.which("rg") is None:
        # Double check logic: if rg ran but returned nothing, archives_to_search is empty, which is correct.
        # Only fallback walk if rg completely failed to run (FileNotFoundError).
        pass

    if not archives_to_search:
        return []

    print(f"Deep searching {len(archives_to_search)} archives...", file=sys.stderr)

    results = []
    # Use fewer threads since we have fewer targets now, but IO is still key
    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = {
            executor.submit(search_archive, query, path): path
            for path in archives_to_search
        }
        for future in futures:
            res = future.result()
            if res:
                results.extend(res)

    return results


def search_flutter_project_deps(query):
    """
    Search in dependencies defined in .dart_tool/package_config.json
    Returns (results, True) if config found and searched, ([], False) otherwise.
    """
    config_path = ".dart_tool/package_config.json"
    if not os.path.exists(config_path):
        return [], False

    print(f"Found {config_path}, scanning project dependencies...", file=sys.stderr)
    try:
        with open(config_path, "r") as f:
            config = json.load(f)

        packages = config.get("packages", [])
        paths_to_search = []

        # Base dir for relative paths in package_config is .dart_tool/
        base_dir = os.path.abspath(".dart_tool")

        for pkg in packages:
            root_uri = pkg.get("rootUri", "")
            # Remove file:// prefix if present
            if root_uri.startswith("file://"):
                path = unquote(root_uri[7:])
            else:
                # Handle relative paths (no scheme usually means relative)
                # But JSON spec says rootUri is a URI.
                # Usually it's "../../../..." or "file:///..."
                path = root_uri

            # If it's relative, resolve it
            if not os.path.isabs(path):
                path = os.path.abspath(os.path.join(base_dir, path))

            if os.path.exists(path):
                paths_to_search.append(path)

        if not paths_to_search:
            return [], True

        print(f"Searching {len(paths_to_search)} package paths...", file=sys.stderr)

        # We can pass all paths to rg at once!
        # rg [flags] pattern path1 path2 ...
        # But command line length limit might be an issue if too many paths.
        # Let's batch them or just iterate if too many.
        # Usually dependencies are < 200, safe for CLI.

        results = []
        # Filter strictly for dart code in dependencies
        flutter_exts = [".dart", ".yaml", ".json"]

        # Split into chunks to be safe (e.g. 50 paths per call)
        chunk_size = 50
        if shutil.which("rg"):
            # Let's do a direct batched rg call here for performance.
            flags = ["-n", "--no-heading", "--color=never"]
            for x in DEFAULT_EXCLUDES:
                flags.append("-g")
                flags.append(f"!**/{x}/*")
            for ext in flutter_exts:
                flags.append("-g")
                flags.append(f"*{ext}")

            cmd_base = ["rg"] + flags + [query]

            for i in range(0, len(paths_to_search), chunk_size):
                chunk = paths_to_search[i : i + chunk_size]
                try:
                    cmd = cmd_base + chunk
                    res = subprocess.run(cmd, capture_output=True, text=True)
                    if res.returncode == 0:
                        results.extend(res.stdout.strip().splitlines())
                except Exception:
                    pass
        else:
            # Fallback to grep by iterating through paths
            print(
                "rg (ripgrep) not found, falling back to grep for package search...",
                file=sys.stderr,
            )
            for path in paths_to_search:
                res = search_directory_rg(query, path, extensions=flutter_exts)
                if res:
                    results.extend(res)

        return results, True

    except Exception as e:
        print(f"Error reading package_config: {e}", file=sys.stderr)
        return [], False


def main():
    parser = argparse.ArgumentParser(description="Search in local dependencies.")
    parser.add_argument("query", help="The string to search for")
    parser.add_argument(
        "--type",
        "-t",
        choices=["all", "ios", "android", "flutter", "dart"],
        default="all",
        help="Filter search by dependency type",
    )

    args = parser.parse_args()
    query = args.query
    search_type = args.type
    results = []

    print(
        f"Searching dependencies for '{query}' (Type: {search_type})...",
        file=sys.stderr,
    )

    # --- iOS ---
    if search_type in ["all", "ios"]:
        start_pods = time.time()
        print("Searching ./Pods...", file=sys.stderr)
        pods_path = "./Pods"
        if os.path.exists(pods_path):
            ios_exts = [".h", ".m", ".mm", ".c", ".cpp", ".swift"]
            res = search_directory_rg(
                query, pods_path, extensions=ios_exts, binary=False
            )
            results.extend(res)

            res_bin = search_directory_rg(query, pods_path, binary=True)
            existing = set(results)
            for r in res_bin:
                if r not in existing:
                    results.append(r)
        end_pods = time.time()
        print(f"Pods search took {end_pods - start_pods:.2f}s", file=sys.stderr)

    # --- Flutter / Dart ---
    if search_type in ["all", "flutter", "dart"]:
        start_pub = time.time()

        # 1. Try precise project-level search first
        project_results, project_searched = search_flutter_project_deps(query)
        if project_searched:
            results.extend(project_results)
            print(
                f"Project dependency search found {len(project_results)} matches.",
                file=sys.stderr,
            )

        # 2. Only fallback to global cache if project config wasn't found
        # (If config WAS found but no results, we trust it - the code isn't in the deps we use.)
        if not project_searched:
            print("Searching ~/.pub-cache (fallback)...", file=sys.stderr)
            pub_path = os.path.expanduser("~/.pub-cache")
            if os.path.exists(pub_path):
                flutter_exts = [".dart", ".yaml", ".json"]
                res = search_directory_rg(query, pub_path, extensions=flutter_exts)
                results.extend(res)

        end_pub = time.time()
        print(f"Flutter/Dart search took {end_pub - start_pub:.2f}s", file=sys.stderr)

    # --- Android ---
    if search_type in ["all", "android"]:
        start_gradle = time.time()
        gradle_res = search_gradle_cache(query)
        results.extend(gradle_res)
        end_gradle = time.time()
        print(f"Gradle search took {end_gradle - start_gradle:.2f}s", file=sys.stderr)

    if not results:
        print("No results found in local dependencies.")
        sys.exit(1)

    for line in results:
        print(line)


if __name__ == "__main__":
    main()
