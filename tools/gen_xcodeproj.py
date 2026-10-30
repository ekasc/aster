#!/usr/bin/env python3
"""Generate the Aster Xcode project.

The project file is generated rather than hand-edited so the source list, the
resource list and the build settings stay in one reviewable place. Run this
after adding or removing files.
"""

from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP_DIR = os.path.join(ROOT, "app")

APP_SOURCES = ["AsterApp.swift", "ContentView.swift", "RuntimeController.swift"]
CORE_SOURCES = ["api.cpp", "cpu.cpp", "elf.cpp", "machine.cpp", "memory.cpp", "platform.cpp"]
CORE_HEADERS = ["aster.h", "clock.h", "cpu.h", "elf.h", "hypercall.h", "machine.h",
                "memory.h", "types.h"]
PAYLOADS = ["demo.elf", "fault_rodata.elf", "fault_unmapped.elf", "fp.elf",
            "hello.elf", "threads.elf"]

TEAM = os.environ.get("ASTER_DEVELOPMENT_TEAM", "3D973X589K")
BUNDLE_ID = os.environ.get("ASTER_BUNDLE_ID", "dev.ekas.aster")

counter = 0


def uid(prefix: str) -> str:
    global counter
    counter += 1
    return f"{prefix}{counter:022X}"


def q(value: str) -> str:
    """Quote a pbxproj value when it contains characters Xcode expects quoted."""
    if all(c.isalnum() or c in "_./-" for c in value) and value:
        return value
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def settings_block(settings: dict[str, str], indent: str = "\t\t\t\t") -> str:
    lines = []
    for key, value in settings.items():
        lines.append(f"{indent}{key} = {q(value)};")
    return "\n".join(lines)


def build_settings(target: bool) -> str:
    common = {
        "ALWAYS_SEARCH_USER_PATHS": "NO",
        "CLANG_ENABLE_MODULES": "YES",
        "CLANG_ENABLE_OBJC_ARC": "YES",
        "CLANG_WARN_DOCUMENTATION_COMMENTS": "YES",
        "COPY_PHASE_STRIP": "NO",
        "ENABLE_STRICT_OBJC_MSGSEND": "YES",
        "GCC_C_LANGUAGE_STANDARD": "gnu17",
        "GCC_NO_COMMON_BLOCKS": "YES",
        "IPHONEOS_DEPLOYMENT_TARGET": "18.0",
        "SDKROOT": "iphoneos",
        "SWIFT_VERSION": "5.0",
        "TARGETED_DEVICE_FAMILY": "1",
    }
    if not target:
        common.update({
            "DEBUG_INFORMATION_FORMAT": "dwarf",
            "ENABLE_TESTABILITY": "YES",
            "GCC_OPTIMIZATION_LEVEL": "0",
            "MTL_ENABLE_DEBUG_INFO": "INCLUDE_SOURCE",
            "ONLY_ACTIVE_ARCH": "YES",
            "SWIFT_ACTIVE_COMPILATION_CONDITIONS": "DEBUG",
            "SWIFT_OPTIMIZATION_LEVEL": "-Onone",
        })
    else:
        common.update({
            "CLANG_CXX_LANGUAGE_STANDARD": "c++17",
            "CLANG_CXX_LIBRARY": "libc++",
            "CODE_SIGN_IDENTITY": "Apple Development",
            "CODE_SIGN_STYLE": "Automatic",
            "CURRENT_PROJECT_VERSION": "1",
            "DEVELOPMENT_TEAM": TEAM,
            "ENABLE_USER_SCRIPT_SANDBOXING": "YES",
            "GENERATE_INFOPLIST_FILE": "NO",
            "HEADER_SEARCH_PATHS": "$(SRCROOT)/../core/include",
            "INFOPLIST_FILE": "Aster/Info.plist",
            "LD_RUNPATH_SEARCH_PATHS": "$(inherited) @executable_path/Frameworks",
            "MARKETING_VERSION": "0.1",
            "PRODUCT_BUNDLE_IDENTIFIER": BUNDLE_ID,
            "PRODUCT_NAME": "$(TARGET_NAME)",
            "SUPPORTED_PLATFORMS": "iphoneos",
            "SUPPORTS_MACCATALYST": "NO",
            "SWIFT_EMIT_LOC_STRINGS": "YES",
            "SWIFT_OBJC_BRIDGING_HEADER": "Aster/Aster-Bridging-Header.h",
        })
    return settings_block(common)


def main() -> int:
    if not os.path.isdir(os.path.join(APP_DIR, "Aster")):
        print("app/Aster is missing", file=sys.stderr)
        return 1

    files = []          # (file_ref_uid, build_file_uid_or_none, name, path, type, group)
    for name in APP_SOURCES:
        ref = uid("F")
        bf = uid("B")
        files.append({"ref": ref, "build": bf, "name": name, "group": "app",
                      "type": "sourcecode.swift"})
    for name in CORE_SOURCES:
        ref = uid("F")
        bf = uid("B")
        files.append({"ref": ref, "build": bf, "name": name, "group": "core-src",
                      "type": "sourcecode.cpp.cpp"})
    for name in CORE_HEADERS:
        ref = uid("F")
        files.append({"ref": ref, "build": None, "name": name, "group": "core-inc",
                      "type": "sourcecode.c.h"})
    for name in PAYLOADS:
        ref = uid("F")
        bf = uid("B")
        files.append({"ref": ref, "build": bf, "name": name, "group": "payloads",
                      "type": "file"})

    bridging_ref = uid("F")
    plist_ref = uid("F")
    product_ref = uid("F")
    app_group = uid("G")
    core_group = uid("G")
    src_group = uid("G")
    inc_group = uid("G")
    payload_group = uid("G")
    products_group = uid("G")
    root_group = uid("G")
    target_uid = uid("T")
    project_uid = uid("P")
    sources_phase = uid("S")
    frameworks_phase = uid("S")
    resources_phase = uid("S")
    target_config_list = uid("C")
    project_config_list = uid("C")
    debug_target = uid("X")
    release_target = uid("X")
    debug_project = uid("X")
    release_project = uid("X")

    out = []
    out.append("// !$*UTF8*$!\n{\n\tarchiveVersion = 1;\n\tclasses = {\n\t};\n\tobjectVersion = 56;\n\tobjects = {\n")

    # PBXBuildFile
    out.append("/* Begin PBXBuildFile section */\n")
    for f in files:
        if f["build"] is None:
            continue
        out.append(f"\t\t{f['build']} /* {f['name']} in {'Resources' if f['group'] == 'payloads' else 'Sources'} */ = {{isa = PBXBuildFile; fileRef = {f['ref']} /* {f['name']} */; }};\n")
    out.append("/* End PBXBuildFile section */\n\n")

    # PBXFileReference
    out.append("/* Begin PBXFileReference section */\n")
    for f in files:
        if f["group"] == "payloads":
            path = f"../build/guest/{f['name']}"
            source_tree = "SOURCE_ROOT"
        elif f["group"] == "core-src":
            path = f["name"]
            source_tree = "<group>"
        elif f["group"] == "core-inc":
            path = f["name"]
            source_tree = "<group>"
        else:
            path = f["name"]
            source_tree = "<group>"
        out.append(f"\t\t{f['ref']} /* {f['name']} */ = {{isa = PBXFileReference; lastKnownFileType = {f['type']}; path = {q(path)}; sourceTree = {q(source_tree)}; }};\n")
    out.append(f"\t\t{bridging_ref} /* Aster-Bridging-Header.h */ = {{isa = PBXFileReference; lastKnownFileType = sourcecode.c.h; path = Aster-Bridging-Header.h; sourceTree = \"<group>\"; }};\n")
    out.append(f"\t\t{plist_ref} /* Info.plist */ = {{isa = PBXFileReference; lastKnownFileType = text.plist.xml; path = Info.plist; sourceTree = \"<group>\"; }};\n")
    out.append(f"\t\t{product_ref} /* Aster.app */ = {{isa = PBXFileReference; explicitFileType = wrapper.application; includeInIndex = 0; path = Aster.app; sourceTree = BUILT_PRODUCTS_DIR; }};\n")
    out.append("/* End PBXFileReference section */\n\n")

    # PBXFrameworksBuildPhase
    out.append("/* Begin PBXFrameworksBuildPhase section */\n")
    out.append(f"\t\t{frameworks_phase} /* Frameworks */ = {{\n\t\t\tisa = PBXFrameworksBuildPhase;\n\t\t\tbuildActionMask = 2147483647;\n\t\t\tfiles = (\n\t\t\t);\n\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n")
    out.append("/* End PBXFrameworksBuildPhase section */\n\n")

    # PBXGroup
    out.append("/* Begin PBXGroup section */\n")

    app_children = [f"\t\t\t\t{f['ref']} /* {f['name']} */,\n" for f in files if f["group"] == "app"]
    app_children.append(f"\t\t\t\t{bridging_ref} /* Aster-Bridging-Header.h */,\n")
    app_children.append(f"\t\t\t\t{plist_ref} /* Info.plist */,\n")
    out.append(f"\t\t{app_group} /* Aster */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n{''.join(app_children)}\t\t\t);\n\t\t\tpath = Aster;\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")

    src_children = [f"\t\t\t\t{f['ref']} /* {f['name']} */,\n" for f in files if f["group"] == "core-src"]
    out.append(f"\t\t{src_group} /* src */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n{''.join(src_children)}\t\t\t);\n\t\t\tpath = src;\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")

    inc_children = [f"\t\t\t\t{f['ref']} /* {f['name']} */,\n" for f in files if f["group"] == "core-inc"]
    out.append(f"\t\t{inc_group} /* aster */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n{''.join(inc_children)}\t\t\t);\n\t\t\tpath = aster;\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")

    out.append(f"\t\t{core_group} /* core */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n\t\t\t\t{inc_group} /* aster */,\n\t\t\t\t{src_group} /* src */,\n\t\t\t);\n\t\t\tpath = ../core;\n\t\t\tsourceTree = SOURCE_ROOT;\n\t\t}};\n")

    payload_children = [f"\t\t\t\t{f['ref']} /* {f['name']} */,\n" for f in files if f["group"] == "payloads"]
    out.append(f"\t\t{payload_group} /* guest */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n{''.join(payload_children)}\t\t\t);\n\t\t\tname = \"Guest payloads\";\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")

    out.append(f"\t\t{products_group} /* Products */ = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n\t\t\t\t{product_ref} /* Aster.app */,\n\t\t\t);\n\t\t\tname = Products;\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")

    out.append(f"\t\t{root_group} = {{\n\t\t\tisa = PBXGroup;\n\t\t\tchildren = (\n\t\t\t\t{app_group} /* Aster */,\n\t\t\t\t{core_group} /* core */,\n\t\t\t\t{payload_group} /* guest */,\n\t\t\t\t{products_group} /* Products */,\n\t\t\t);\n\t\t\tsourceTree = \"<group>\";\n\t\t}};\n")
    out.append("/* End PBXGroup section */\n\n")

    # PBXNativeTarget
    out.append("/* Begin PBXNativeTarget section */\n")
    out.append(f"""\t\t{target_uid} /* Aster */ = {{
\t\t\tisa = PBXNativeTarget;
\t\t\tbuildConfigurationList = {target_config_list} /* Build configuration list for PBXNativeTarget "Aster" */;
\t\t\tbuildPhases = (
\t\t\t\t{sources_phase} /* Sources */,
\t\t\t\t{frameworks_phase} /* Frameworks */,
\t\t\t\t{resources_phase} /* Resources */,
\t\t\t);
\t\t\tbuildRules = (
\t\t\t);
\t\t\tdependencies = (
\t\t\t);
\t\t\tname = Aster;
\t\t\tproductName = Aster;
\t\t\tproductReference = {product_ref} /* Aster.app */;
\t\t\tproductType = "com.apple.product-type.application";
\t\t}};
""")
    out.append("/* End PBXNativeTarget section */\n\n")

    # PBXProject
    out.append("/* Begin PBXProject section */\n")
    out.append(f"""\t\t{project_uid} /* Project object */ = {{
\t\t\tisa = PBXProject;
\t\t\tattributes = {{
\t\t\t\tBuildIndependentTargetsInParallel = 1;
\t\t\t\tLastSwiftUpdateCheck = 2700;
\t\t\t\tLastUpgradeCheck = 2700;
\t\t\t\tTargetAttributes = {{
\t\t\t\t\t{target_uid} = {{
\t\t\t\t\t\tCreatedOnToolsVersion = 27.0;
\t\t\t\t\t}};
\t\t\t\t}};
\t\t\t}};
\t\t\tbuildConfigurationList = {project_config_list} /* Build configuration list for PBXProject "Aster" */;
\t\t\tcompatibilityVersion = "Xcode 14.0";
\t\t\tdevelopmentRegion = en;
\t\t\thasScannedForEncodings = 0;
\t\t\tknownRegions = (
\t\t\t\ten,
\t\t\t\tBase,
\t\t\t);
\t\t\tmainGroup = {root_group};
\t\t\tproductRefGroup = {products_group} /* Products */;
\t\t\tprojectDirPath = "";
\t\t\tprojectRoot = "";
\t\t\ttargets = (
\t\t\t\t{target_uid} /* Aster */,
\t\t\t);
\t\t}};
""")
    out.append("/* End PBXProject section */\n\n")

    # Resources phase
    out.append("/* Begin PBXResourcesBuildPhase section */\n")
    res_files = [f"\t\t\t\t{f['build']} /* {f['name']} in Resources */,\n" for f in files if f["group"] == "payloads"]
    out.append(f"\t\t{resources_phase} /* Resources */ = {{\n\t\t\tisa = PBXResourcesBuildPhase;\n\t\t\tbuildActionMask = 2147483647;\n\t\t\tfiles = (\n{''.join(res_files)}\t\t\t);\n\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n")
    out.append("/* End PBXResourcesBuildPhase section */\n\n")

    # Sources phase
    out.append("/* Begin PBXSourcesBuildPhase section */\n")
    src_files = [f"\t\t\t\t{f['build']} /* {f['name']} in Sources */,\n" for f in files if f["build"] and f["group"] != "payloads"]
    out.append(f"\t\t{sources_phase} /* Sources */ = {{\n\t\t\tisa = PBXSourcesBuildPhase;\n\t\t\tbuildActionMask = 2147483647;\n\t\t\tfiles = (\n{''.join(src_files)}\t\t\t);\n\t\t\trunOnlyForDeploymentPostprocessing = 0;\n\t\t}};\n")
    out.append("/* End PBXSourcesBuildPhase section */\n\n")

    # Build configurations
    out.append("/* Begin XCBuildConfiguration section */\n")
    out.append(f"\t\t{debug_project} /* Debug */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n{build_settings(False)}\n\t\t\t}};\n\t\t\tname = Debug;\n\t\t}};\n")
    out.append(f"\t\t{release_project} /* Release */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n{build_settings(False)}\n\t\t\t\tDEBUG_INFORMATION_FORMAT = \"dwarf-with-dsym\";\n\t\t\t\tENABLE_NS_ASSERTIONS = NO;\n\t\t\t\tGCC_OPTIMIZATION_LEVEL = 3;\n\t\t\t\tSWIFT_COMPILATION_MODE = wholemodule;\n\t\t\t\tVALIDATE_PRODUCT = YES;\n\t\t\t}};\n\t\t\tname = Release;\n\t\t}};\n")
    out.append(f"\t\t{debug_target} /* Debug */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n{build_settings(True)}\n\t\t\t}};\n\t\t\tname = Debug;\n\t\t}};\n")
    out.append(f"\t\t{release_target} /* Release */ = {{\n\t\t\tisa = XCBuildConfiguration;\n\t\t\tbuildSettings = {{\n{build_settings(True)}\n\t\t\t\tGCC_OPTIMIZATION_LEVEL = 3;\n\t\t\t\tSWIFT_OPTIMIZATION_LEVEL = \"-O\";\n\t\t\t}};\n\t\t\tname = Release;\n\t\t}};\n")
    out.append("/* End XCBuildConfiguration section */\n\n")

    # Configuration lists
    out.append("/* Begin XCConfigurationList section */\n")
    out.append(f"\t\t{project_config_list} /* Build configuration list for PBXProject \"Aster\" */ = {{\n\t\t\tisa = XCConfigurationList;\n\t\t\tbuildConfigurations = (\n\t\t\t\t{debug_project} /* Debug */,\n\t\t\t\t{release_project} /* Release */,\n\t\t\t);\n\t\t\tdefaultConfigurationIsVisible = 0;\n\t\t\tdefaultConfigurationName = Release;\n\t\t}};\n")
    out.append(f"\t\t{target_config_list} /* Build configuration list for PBXNativeTarget \"Aster\" */ = {{\n\t\t\tisa = XCConfigurationList;\n\t\t\tbuildConfigurations = (\n\t\t\t\t{debug_target} /* Debug */,\n\t\t\t\t{release_target} /* Release */,\n\t\t\t);\n\t\t\tdefaultConfigurationIsVisible = 0;\n\t\t\tdefaultConfigurationName = Release;\n\t\t}};\n")
    out.append("/* End XCConfigurationList section */\n")

    out.append("\t};\n\trootObject = " + project_uid + " /* Project object */;\n}\n")

    project_dir = os.path.join(APP_DIR, "Aster.xcodeproj")
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "project.pbxproj"), "w") as fh:
        fh.write("".join(out))

    scheme_dir = os.path.join(project_dir, "xcshareddata", "xcschemes")
    os.makedirs(scheme_dir, exist_ok=True)
    with open(os.path.join(scheme_dir, "Aster.xcscheme"), "w") as fh:
        fh.write(f"""<?xml version="1.0" encoding="UTF-8"?>
<Scheme LastUpgradeVersion="2700" version="1.7">
   <BuildAction parallelizeBuildables="YES" buildImplicitDependencies="YES">
      <BuildActionEntries>
         <BuildActionEntry buildForTesting="YES" buildForRunning="YES" buildForProfiling="YES" buildForArchiving="YES" buildForAnalyzing="YES">
            <BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target_uid}" BuildableName="Aster.app" BlueprintName="Aster" ReferencedContainer="container:Aster.xcodeproj">
            </BuildableReference>
         </BuildActionEntry>
      </BuildActionEntries>
   </BuildAction>
   <TestAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.DebuggerFoundation.Launcher.LLDB" shouldUseLaunchSchemeArgsEnv="YES">
      <Testables>
      </Testables>
   </TestAction>
   <LaunchAction buildConfiguration="Debug" selectedDebuggerIdentifier="Xcode.DebuggerFoundation.Debugger.LLDB" selectedLauncherIdentifier="Xcode.DebuggerFoundation.Launcher.LLDB" launchStyle="0" useCustomWorkingDirectory="NO" ignoresPersistentStateOnLaunch="NO" debugDocumentVersioning="YES" debugServiceExtension="internal" allowLocationSimulation="YES">
      <BuildableProductRunnable runnableDebuggingMode="0">
         <BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target_uid}" BuildableName="Aster.app" BlueprintName="Aster" ReferencedContainer="container:Aster.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </LaunchAction>
   <ProfileAction buildConfiguration="Release" shouldUseLaunchSchemeArgsEnv="YES" savedToolIdentifier="" useCustomWorkingDirectory="NO" debugDocumentVersioning="YES">
      <BuildableProductRunnable runnableDebuggingMode="0">
         <BuildableReference BuildableIdentifier="primary" BlueprintIdentifier="{target_uid}" BuildableName="Aster.app" BlueprintName="Aster" ReferencedContainer="container:Aster.xcodeproj">
         </BuildableReference>
      </BuildableProductRunnable>
   </ProfileAction>
   <AnalyzeAction buildConfiguration="Debug">
   </AnalyzeAction>
   <ArchiveAction buildConfiguration="Release" revealArchiveInOrganizer="YES">
   </ArchiveAction>
</Scheme>
""")

    print(f"wrote {project_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
