"""MCP workflow prompts are templates, not automatic scene operations."""
import asyncio
from omniverse_scene_mcp import workflows


def test_all_workflow_templates_render_without_unfilled_parameters():
    names = workflows.prompt_names()
    assert names
    for name in names:
        rendered = workflows.render_prompt(name)
        assert isinstance(rendered, str) and len(rendered) > 80
        for parameter in workflows.prompt_params(name):
            assert '{' + parameter + '}' not in rendered


def test_workflows_register_with_real_fastmcp():
    from mcp.server.fastmcp import FastMCP
    app = FastMCP('workflow-tests')
    names = workflows.register_prompts(app)
    listed = asyncio.run(app.list_prompts())
    assert set(names) == set(workflows.prompt_names()) == {p.name for p in listed}
    assert workflows.register_prompts(object()) == []
