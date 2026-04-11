"""Round 22 - Tests for maya-scripting, maya-utility, maya-pipeline.

Covers all 12 new scripts (4 per skill domain).
Scripts use the project-standard pattern: named args, lazy imports of
``maya.cmds`` / ``maya.mel``, and return ``ActionResultModel.to_dict()``.
"""

# Import built-in modules
import importlib.util
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

SKILLS_ROOT = Path(__file__).parent.parent / "src" / "dcc_mcp_maya" / "skills"


def _load(skill_dir, script_name):
    """Load a skill script module by path with a unique module name."""
    script_path = SKILLS_ROOT / skill_dir / "scripts" / "{}.py".format(script_name)
    spec = importlib.util.spec_from_file_location(
        "r22_{}_{}".format(skill_dir.replace("-", "_"), script_name),
        str(script_path),
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _mock_maya(cmds_attrs=None, mel_attrs=None):
    mc = MagicMock()
    mock_mel = MagicMock()
    mm = MagicMock()
    mm.cmds = mc
    mm.mel = mock_mel
    if cmds_attrs:
        for k, v in cmds_attrs.items():
            setattr(mc, k, v)
    if mel_attrs:
        for k, v in mel_attrs.items():
            setattr(mock_mel, k, v)
    return mm, mc, mock_mel


# ===========================================================================
# maya-scripting / execute_mel
# ===========================================================================

class TestExecuteMel:
    def test_empty_code_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "execute_mel")
            result = mod.execute_mel("")
        assert result["success"] is False

    def test_whitespace_only_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "execute_mel")
            result = mod.execute_mel("   ")
        assert result["success"] is False

    def test_successful_mel(self):
        mm, mc, mock_mel = _mock_maya()
        mock_mel.eval.return_value = "sphere1"
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "execute_mel")
            result = mod.execute_mel("polySphere -n sphere1;")
        assert result["success"] is True
        assert result["context"]["output"] == "sphere1"

    def test_mel_returns_none(self):
        mm, mc, mock_mel = _mock_maya()
        mock_mel.eval.return_value = None
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "execute_mel")
            result = mod.execute_mel("print 1;")
        assert result["success"] is True
        assert result["context"]["output"] == ""

    def test_mel_exception(self):
        mm, mc, mock_mel = _mock_maya()
        mock_mel.eval.side_effect = RuntimeError("MEL error")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "execute_mel")
            result = mod.execute_mel("badMEL;")
        assert result["success"] is False
        assert "MEL error" in str(result)


# ===========================================================================
# maya-scripting / execute_python
# ===========================================================================

class TestExecutePython:
    def test_empty_code_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "execute_python")
            result = mod.execute_python("")
        assert result["success"] is False

    def test_valid_python(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "execute_python")
            result = mod.execute_python("x = 1 + 1")
        assert result["success"] is True

    def test_capture_output(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "execute_python")
            result = mod.execute_python("print('hello')", capture_output=True)
        assert result["success"] is True
        assert "hello" in result["context"]["stdout"]

    def test_syntax_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "execute_python")
            result = mod.execute_python("def broken(")
        assert result["success"] is False

    def test_runtime_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "execute_python")
            result = mod.execute_python("raise ValueError('boom')")
        assert result["success"] is False
        assert "boom" in str(result)


# ===========================================================================
# maya-scripting / list_mel_procedures
# ===========================================================================

class TestListMelProcedures:
    def test_returns_list(self):
        mm, mc, mock_mel = _mock_maya()
        mock_mel.eval.side_effect = [
            "",  # warm-up whatIs call
            ["doCreateSphere", "doPolyCube", "doSomethingElse"],  # globalProcs()
        ]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "list_mel_procedures")
            result = mod.list_mel_procedures()
        assert result["success"] is True
        assert result["context"]["count"] == 3

    def test_pattern_filter(self):
        mm, mc, mock_mel = _mock_maya()
        mock_mel.eval.side_effect = [
            "",
            ["doPolyCube", "doPolySphere", "doSomething"],
        ]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "list_mel_procedures")
            result = mod.list_mel_procedures(pattern="poly")
        assert result["success"] is True
        assert result["context"]["count"] == 2

    def test_limit_applied(self):
        mm, mc, mock_mel = _mock_maya()
        mock_mel.eval.side_effect = [
            "",
            ["proc{}".format(i) for i in range(100)],
        ]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "list_mel_procedures")
            result = mod.list_mel_procedures(limit=10)
        assert result["success"] is True
        assert result["context"]["count"] <= 10

    def test_mel_eval_exception(self):
        mm, mc, mock_mel = _mock_maya()
        mock_mel.eval.side_effect = RuntimeError("not available")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-scripting", "list_mel_procedures")
            result = mod.list_mel_procedures()
        assert result["success"] is False


# ===========================================================================
# maya-scripting / get_script_node
# ===========================================================================

class TestGetScriptNode:
    def test_no_name_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "get_script_node")
            result = mod.get_script_node("")
        assert result["success"] is False

    def test_get_existing(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.getAttr.side_effect = lambda plug: "print('hi')" if ".before" in plug else 0
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "get_script_node")
            result = mod.get_script_node("myScript", action="get")
        assert result["success"] is True
        assert result["context"]["script_node"]["name"] == "myScript"

    def test_get_nonexistent(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = False
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "get_script_node")
            result = mod.get_script_node("missing", action="get")
        assert result["success"] is False

    def test_create_script_node(self):
        mm, mc, mock_mel = _mock_maya()
        mc.scriptNode.return_value = "myScript"
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "get_script_node")
            result = mod.get_script_node("myScript", action="create", script="print(1)")
        assert result["success"] is True
        assert result["context"]["script_node"]["name"] == "myScript"

    def test_create_missing_script_body(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "get_script_node")
            result = mod.get_script_node("myScript", action="create")
        assert result["success"] is False

    def test_delete_script_node(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "get_script_node")
            result = mod.get_script_node("myScript", action="delete")
        assert result["success"] is True

    def test_unknown_action(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-scripting", "get_script_node")
            result = mod.get_script_node("myScript", action="fly")
        assert result["success"] is False


# ===========================================================================
# maya-utility / create_utility_node
# ===========================================================================

class TestCreateUtilityNode:
    def test_no_type_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "create_utility_node")
            result = mod.create_utility_node("")
        assert result["success"] is False

    def test_create_without_name(self):
        mm, mc, mock_mel = _mock_maya()
        mc.shadingNode.return_value = "multiplyDivide1"
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "create_utility_node")
            result = mod.create_utility_node("multiplyDivide")
        assert result["success"] is True
        assert result["context"]["node_name"] == "multiplyDivide1"
        assert result["context"]["node_type"] == "multiplyDivide"

    def test_create_with_name(self):
        mm, mc, mock_mel = _mock_maya()
        mc.shadingNode.return_value = "multiplyDivide1"
        mc.rename.return_value = "myMult"
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "create_utility_node")
            result = mod.create_utility_node("multiplyDivide", name="myMult")
        assert result["success"] is True
        mc.shadingNode.assert_called_once_with("multiplyDivide", asUtility=True)
        mc.rename.assert_called_once_with("multiplyDivide1", "myMult")

    def test_create_with_connection(self):
        mm, mc, mock_mel = _mock_maya()
        mc.shadingNode.return_value = "rev1"
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "create_utility_node")
            result = mod.create_utility_node("reverse", connect_from="lambert1.outColor")
        assert result["success"] is True
        mc.connectAttr.assert_called_once()

    def test_shading_node_exception(self):
        mm, mc, mock_mel = _mock_maya()
        mc.shadingNode.side_effect = RuntimeError("invalid type")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "create_utility_node")
            result = mod.create_utility_node("badType")
        assert result["success"] is False


# ===========================================================================
# maya-utility / get_scene_statistics
# ===========================================================================

class TestGetSceneStatistics:
    def test_basic_stats(self):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.side_effect = lambda **kw: (
            ["pSphere1", "pCube1"] if kw.get("type") == "mesh"
            else ["transform1"] if kw.get("type") == "transform"
            else ["n1", "n2", "n3", "n4", "n5"]
        )
        mc.polyEvaluate.return_value = 100
        mc.memory.return_value = 2048
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "get_scene_statistics")
            result = mod.get_scene_statistics()
        assert result["success"] is True
        ctx = result["context"]
        assert "mesh_count" in ctx
        assert "poly_vertex_count" in ctx

    def test_no_memory(self):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.return_value = []
        mc.polyEvaluate.return_value = 0
        mc.memory.side_effect = RuntimeError("no memory")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "get_scene_statistics")
            result = mod.get_scene_statistics(include_memory=True)
        assert result["success"] is True
        assert result["context"]["memory_mb"] is None

    def test_extra_node_types(self):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.return_value = []
        mc.polyEvaluate.return_value = 0
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "get_scene_statistics")
            result = mod.get_scene_statistics(node_types=["camera", "joint"])
        assert result["success"] is True
        assert "camera_count" in result["context"]

    def test_ls_exception(self):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.side_effect = RuntimeError("scene not loaded")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "get_scene_statistics")
            result = mod.get_scene_statistics()
        assert result["success"] is False


# ===========================================================================
# maya-utility / list_node_connections
# ===========================================================================

class TestListNodeConnections:
    def test_no_node_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "list_node_connections")
            result = mod.list_node_connections("")
        assert result["success"] is False

    def test_node_not_found(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = False
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "list_node_connections")
            result = mod.list_node_connections("missing")
        assert result["success"] is False

    def test_both_directions(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        # connections=True, plugs=True → [dst0, src0, dst1, src1, ...]
        mc.listConnections.side_effect = [
            ["lambert1.outColor", "file1.outColor"],    # incoming pairs
            ["lambert1.color", "shaderGlow1.glowColor"],  # outgoing pairs
        ]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "list_node_connections")
            result = mod.list_node_connections("lambert1", direction="both")
        assert result["success"] is True
        assert result["context"]["count"] == 2

    def test_incoming_only(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.listConnections.return_value = ["lambert1.outColor", "file1.outColor"]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "list_node_connections")
            result = mod.list_node_connections("lambert1", direction="incoming")
        assert result["success"] is True

    def test_outgoing_only(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.listConnections.return_value = ["lambert1.color", "shaderGlow1.glowColor"]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "list_node_connections")
            result = mod.list_node_connections("lambert1", direction="outgoing")
        assert result["success"] is True

    def test_exception(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.listConnections.side_effect = RuntimeError("oops")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "list_node_connections")
            result = mod.list_node_connections("lambert1")
        assert result["success"] is False


# ===========================================================================
# maya-utility / clean_scene
# ===========================================================================

class TestCleanScene:
    def test_dry_run(self):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.side_effect = lambda type=None: (
            ["unknownNode1"] if type == "unknown"
            else ["layer1"] if type == "displayLayer"
            else []
        )
        mc.unknownPlugin.return_value = ["plug1"]
        mc.editDisplayLayerMembers.return_value = []
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "clean_scene")
            result = mod.clean_scene(dry_run=True)
        assert result["success"] is True
        assert result["context"]["removed_count"] == 0
        assert result["context"]["flagged_count"] > 0

    def test_remove_unknown_nodes(self):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.side_effect = lambda type=None: (
            ["unknownNode1"] if type == "unknown"
            else ["defaultLayer"] if type == "displayLayer"
            else []
        )
        mc.unknownPlugin.return_value = []
        mc.editDisplayLayerMembers.return_value = ["obj1"]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "clean_scene")
            result = mod.clean_scene(
                remove_unknown_nodes=True,
                remove_unknown_plugins=False,
                remove_empty_display_layers=True,
            )
        assert result["success"] is True
        mc.delete.assert_called()

    def test_no_cleanup_needed(self):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.return_value = []
        mc.unknownPlugin.return_value = []
        mc.editDisplayLayerMembers.return_value = ["obj1"]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "clean_scene")
            result = mod.clean_scene()
        assert result["success"] is True
        assert result["context"]["removed_count"] == 0

    def test_exception(self):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.side_effect = RuntimeError("crash")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-utility", "clean_scene")
            result = mod.clean_scene()
        assert result["success"] is False


# ===========================================================================
# maya-pipeline / set_project
# ===========================================================================

class TestSetProject:
    def test_no_path_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "set_project")
            result = mod.set_project("")
        assert result["success"] is False

    def test_nonexistent_dir_no_create(self, tmp_path):
        missing = str(tmp_path / "nonexistent" / "proj")
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "set_project")
            result = mod.set_project(missing)
        assert result["success"] is False

    def test_nonexistent_dir_with_create(self, tmp_path):
        new_dir = str(tmp_path / "new_project")
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "set_project")
            result = mod.set_project(new_dir, create_if_missing=True)
        assert result["success"] is True
        assert os.path.isdir(new_dir)

    def test_existing_dir(self, tmp_path):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "set_project")
            result = mod.set_project(str(tmp_path))
        assert result["success"] is True
        assert result["context"]["project_path"] == str(tmp_path)

    def test_cmds_workspace_exception(self, tmp_path):
        mm, mc, mock_mel = _mock_maya()
        mc.workspace.side_effect = RuntimeError("workspace error")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "set_project")
            result = mod.set_project(str(tmp_path))
        assert result["success"] is False


# ===========================================================================
# maya-pipeline / publish_asset
# ===========================================================================

class TestPublishAsset:
    def test_no_asset_name_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-pipeline", "publish_asset")
            result = mod.publish_asset("", publish_dir="/tmp")
        assert result["success"] is False

    def test_no_publish_dir_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-pipeline", "publish_asset")
            result = mod.publish_asset("hero", publish_dir="")
        assert result["success"] is False

    def test_nothing_selected_error(self, tmp_path):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.return_value = []
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-pipeline", "publish_asset")
            result = mod.publish_asset("hero", publish_dir=str(tmp_path))
        assert result["success"] is False

    def test_publish_ma_format(self, tmp_path):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.return_value = ["pSphere1"]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-pipeline", "publish_asset")
            result = mod.publish_asset("hero", publish_dir=str(tmp_path), format="ma")
        assert result["success"] is True
        assert result["context"]["version"] == 1
        assert result["context"]["publish_path"].endswith(".ma")

    def test_publish_fbx_format(self, tmp_path):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.return_value = ["pSphere1"]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-pipeline", "publish_asset")
            result = mod.publish_asset("hero", publish_dir=str(tmp_path), format="fbx")
        assert result["success"] is True
        assert result["context"]["publish_path"].endswith(".fbx")

    def test_explicit_version(self, tmp_path):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.return_value = ["pCube1"]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-pipeline", "publish_asset")
            result = mod.publish_asset(
                "prop", publish_dir=str(tmp_path), format="ma", version=5
            )
        assert result["success"] is True
        assert result["context"]["version"] == 5

    def test_unsupported_format(self, tmp_path):
        mm, mc, mock_mel = _mock_maya()
        mc.ls.return_value = ["pSphere1"]
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc, "maya.mel": mock_mel}):
            mod = _load("maya-pipeline", "publish_asset")
            result = mod.publish_asset(
                "hero", publish_dir=str(tmp_path), format="abc"
            )
        assert result["success"] is False


# ===========================================================================
# maya-pipeline / tag_asset_metadata
# ===========================================================================

class TestTagAssetMetadata:
    def test_no_node_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "tag_asset_metadata")
            result = mod.tag_asset_metadata("")
        assert result["success"] is False

    def test_node_not_found(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = False
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "tag_asset_metadata")
            result = mod.tag_asset_metadata("missing", asset_name="hero")
        assert result["success"] is False

    def test_no_metadata_provided(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "tag_asset_metadata")
            result = mod.tag_asset_metadata("pSphere1")
        assert result["success"] is False

    def test_tag_creates_attr_if_missing(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.attributeQuery.return_value = False  # attr doesn't exist → must addAttr
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "tag_asset_metadata")
            result = mod.tag_asset_metadata(
                "pSphere1", asset_name="hero", asset_version="v001"
            )
        assert result["success"] is True
        assert mc.addAttr.call_count >= 2

    def test_tag_existing_attr(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.attributeQuery.return_value = True  # attr already exists → no addAttr
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "tag_asset_metadata")
            result = mod.tag_asset_metadata("pSphere1", pipeline_step="rigging")
        assert result["success"] is True
        mc.addAttr.assert_not_called()

    def test_setattr_exception(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.attributeQuery.return_value = True
        mc.setAttr.side_effect = RuntimeError("locked")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "tag_asset_metadata")
            result = mod.tag_asset_metadata("pSphere1", asset_name="hero")
        assert result["success"] is False


# ===========================================================================
# maya-pipeline / get_asset_metadata
# ===========================================================================

class TestGetAssetMetadata:
    def test_no_node_error(self):
        mm, mc, mock_mel = _mock_maya()
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "get_asset_metadata")
            result = mod.get_asset_metadata("")
        assert result["success"] is False

    def test_node_not_found(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = False
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "get_asset_metadata")
            result = mod.get_asset_metadata("missing")
        assert result["success"] is False

    def test_no_attrs_tagged(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.attributeQuery.return_value = False
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "get_asset_metadata")
            result = mod.get_asset_metadata("pSphere1")
        assert result["success"] is True
        assert result["context"]["tagged_count"] == 0

    def test_some_attrs_tagged(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True

        def attr_exists(attr, node, exists):
            return attr in ("asset_name", "asset_version")

        mc.attributeQuery.side_effect = attr_exists
        mc.getAttr.side_effect = lambda plug: "hero" if "asset_name" in plug else "v003"
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "get_asset_metadata")
            result = mod.get_asset_metadata("pSphere1")
        assert result["success"] is True
        assert result["context"]["tagged_count"] == 2
        assert result["context"]["metadata"]["asset_name"] == "hero"

    def test_getattr_exception(self):
        mm, mc, mock_mel = _mock_maya()
        mc.objExists.return_value = True
        mc.attributeQuery.side_effect = RuntimeError("attr query failed")
        with patch.dict(sys.modules, {"maya": mm, "maya.cmds": mc}):
            mod = _load("maya-pipeline", "get_asset_metadata")
            result = mod.get_asset_metadata("pSphere1")
        assert result["success"] is False
