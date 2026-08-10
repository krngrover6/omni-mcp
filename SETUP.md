# Omni-MCP Setup

This guide sets up the full Omniverse MCP workflow.

## What you need

- Git
- Python 3.12+ or the Python that ships with your Omniverse/Kit setup
- USD Composer if you want live scene control
- Access to the `kit-app-template` and `kit-usd-agents` submodules if you want the full stack

## Clone the repo

```powershell
git clone --recurse-submodules https://github.com/krngrover6/omni-mcp.git
cd omni-mcp
```

If the repo is already cloned:

```powershell
git submodule update --init --recursive
```

## Install the MCP package

Go into the MCP folder:

```powershell
cd omniverse_scene_mcp
```

Then install it:

```powershell
pip install -e .
```

If your environment uses a different Python, run the same command with that interpreter.

## Start the Omniverse stack

From the repo root:

```powershell
cd C:\Users\krngr\Downloads\Omniverse
.\start-omniverse.ps1
```

To launch USD Composer with live scene awareness:

```powershell
.\start-omniverse.ps1 -LaunchComposer
```

If you only want the launcher without Composer:

```powershell
.\start-omniverse.ps1
```

## What starts where

- Router MCP: `http://127.0.0.1:9905/mcp`
- Composer live scene bridge: `http://127.0.0.1:9910/rpc`
- Bowerbot: `http://127.0.0.1:8181`

## Use the MCP

Good first commands:

- `router_status`
- `scene_summary`
- `inspect /World`
- `add a cube`
- `make it red`
- `practice transforms with me`
- `quiz me on schemas`
- `what should I do next?`

## If you want live Composer control

1. Open USD Composer
2. Make sure the stage is loaded
3. Run the launcher with `-LaunchComposer`
4. Use the router or natural-language commands against the live scene

## Notes about submodules

- `kit-app-template` is a separate repo used for the Composer setup extension
- `kit-usd-agents` is another separate repo used for Omniverse agent tooling
- If you do not have access to those repos, the main router repo still works, but some live Composer features may be limited

## Quick summary

```text
git clone --recurse-submodules https://github.com/krngrover6/omni-mcp.git
cd omni-mcp
cd omniverse_scene_mcp
pip install -e .
cd C:\Users\krngr\Downloads\Omniverse
.\start-omniverse.ps1 -LaunchComposer
```

