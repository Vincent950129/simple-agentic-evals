"""Out-of-tree ``ale_run`` agent used by the eval-service docker grader.

The package lives under ``eval_service/_ale_agent`` (a dedicated PYTHONPATH root
that holds nothing else) so that when ale_run imports it inside the ALE uv venv,
no other eval-service module name can shadow a stdlib / task import.

Referenced from a generated experiment as
``class: ale_byoa_agent.deployer.ByoaDeployer``.
"""
