"""Your harness settings: add your own here, e.g. agent loop limits, tool limits, context management

The course API settings are environment variables, not config: CS2680_API_KEY and
CS2680_MODEL_EXPERT / CS2680_MODEL_STANDARD / CS2680_MODEL_STARTER (exported on the host,
passed in by run_all.py) and CS2680_BASE_URL (set by run_all.py to the egress proxy, the agent
container's only way to the API). Read them with os.environ; never hard-code the URL.
"""
