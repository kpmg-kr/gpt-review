"""Basic functions for requesting review based goals from GPT."""
import os
from dataclasses import dataclass
from typing import Dict

import yaml
from knack import CLICommandsLoader
from knack.arguments import ArgumentsContext
from knack.commands import CommandGroup

from gpt_review._ask import _ask
from gpt_review._command import GPTCommandGroup
from gpt_review.prompts._prompt import (
    load_bug_yaml,
    load_coverage_yaml,
    load_summary_yaml,
)

_CHECKS = {
    "SUMMARY_CHECKS": [
        {
            "flag": "SUMMARY_SUGGEST",
            "header": "Suggestions",
            "goal": """You are an expert developer, your task is to review a set of pull requests.
You are given a list of filenames and their partial contents, but note that you might not have the full context of the code.
Only review lines of code which have been changed (added or removed) in the pull request. The code looks similar to the output of a git diff command. Lines which have been removed are prefixed with a minus (-) and lines which have been added are prefixed with a plus (+). Other lines are added to provide context but should be ignored in the review.
In your feedback, focus on highlighting potential bugs, improving readability if it is a problem, making code cleaner, and maximising the performance of the programming language.
Flag any API keys or secrets present in the code in plain text immediately as highest risk. Rate the changes based on SOLID principles if applicable.
Do not comment on breaking functions down into smaller, more manageable functions unless it is a huge problem. Also be aware that there will be libraries and techniques used which you are not familiar with, so do not comment on those unless you are confident that there is a problem.
Use markdown formatting for the feedback details. Also do not include the filename or risk level in the feedback details.
Include brief example code snippets in the feedback details for your suggested changes when you're confident your suggestions are improvements. Use the same programming language as the file under review.
If there are multiple improvements you suggest in the feedback details, use an ordered list to indicate the priority of the changes.
Please give your answer in Korean.""",
        },
    ],
    "RISK_CHECKS": [
        {
            "flag": "RISK_BREAKING",
            "header": "Breaking Changes",
            "goal": """Detect breaking changes in a git diff. Here are some things that can cause a breaking change.
- new parameters to public functions which are required and have no default value. Answer me in Korean.
""",
        },
    ],
}


@dataclass
class GitFile:
    """A git file with its diff contents."""

    file_name: str
    diff: str


def _request_goal(git_diff, goal, fast: bool = False, large: bool = False, temperature: float = 0) -> str:
    """
    Request a goal from GPT.

    Args:
        git_diff (str): The git diff to split.
        goal (str): The goal to request from GPT.
        fast (bool, optional): Whether to use the fast model. Defaults to False.
        large (bool, optional): Whether to use the large model. Defaults to False.
        temperature (float, optional): The temperature to use. Defaults to 0.

    Returns:
        response (str): The response from GPT.
    """
    prompt = f"""
{goal}

{git_diff}
"""

    return _ask([prompt], max_tokens=1500, fast=fast, large=large, temperature=temperature)["response"]


def _check_goals(git_diff, checks, indent="###") -> str:
    """
    Check goals.

    Args:
        git_diff (str): The git diff to check.
        checks (list): The checks to run.

    Returns:
        str: The output of the checks.
    """
    return "".join(
        f"""
{indent} {check["header"]}

{_request_goal(git_diff, goal=check["goal"])}
"""
        for check in checks
        if os.getenv(check["flag"], "true").lower() == "true"
    )


def _summarize_pr(git_diff) -> str:
    """
    Summarize a PR.

    Args:
        git_diff (str): The git diff to summarize.

    Returns:
        str: The summary of the PR.
    """
    text = ""
    if os.getenv("FULL_SUMMARY", "true").lower() == "true":
        text += f"""
{_request_goal(git_diff, goal="Below is a code patch, please help me do a very simple code review on it about 5 lines. Do not include code patch content. Please give your answer in Korean. ")}
"""

        text += _check_goals(git_diff, _CHECKS["SUMMARY_CHECKS"])
    return text


def _summarize_file(diff) -> str:
    """Summarize a file in a git diff.

    Args:
        diff (str): The file to summarize.

    Returns:
        str: The summary of the file.
    """
    git_file = GitFile(diff.split(" b/")[0], diff)
    question = load_summary_yaml().format(diff=diff)

    response = _ask(question=[question], temperature=0.0)
    return f"""
### {git_file.file_name}
{response}
"""


def _split_diff(git_diff):
    """Split a git diff into a list of files and their diff contents.

    Args:
        git_diff (str): The git diff to split.

    Returns:
        list: A list of tuples containing the file name and diff contents.
    """
    diff = "diff"
    git = "--git a/"
    return git_diff.split(f"{diff} {git}")[1:]  # Use formated string to prevent splitting


def _summarize_test_coverage(git_diff) -> str:
    """Summarize the test coverage of a git diff.

    Args:
        git_diff (str): The git diff to summarize.

    Returns:
        str: The summary of the test coverage.
    """
    files = {}
    for diff in _split_diff(git_diff):
        path = diff.split(" b/")[0]
        git_file = GitFile(path.split("/")[len(path.split("/")) - 1], diff)

        files[git_file.file_name] = git_file

    question = load_coverage_yaml().format(diff=git_diff)

    return _ask([question], temperature=0.0, max_tokens=1500)["response"]


def _summarize_risk(git_diff) -> str:
    """
    Summarize potential risks.

    Args:
        git_diff (str): The git diff to split.

    Returns:
        response (str): The response from GPT.
    """
    text = ""
    if os.getenv("RISK_SUMMARY", "true").lower() == "true":
        text += """
## Potential Risks

"""
        text += _check_goals(git_diff, _CHECKS["RISK_CHECKS"])
    return text


def _summarize_files(git_diff) -> str:
    """Summarize git files."""
    summary = """
# Summary by GPT
"""

    summary += _summarize_pr(git_diff)

    if os.getenv("FILE_SUMMARY", "true").lower() == "true":
        file_summary = """
## Changes

"""
        file_summary += "".join(_summarize_file(diff) for diff in _split_diff(git_diff))
        if os.getenv("FILE_SUMMARY_FULL", "true").lower() == "true":
            summary += file_summary

        summary += f"""
### Summary of File Changes
{_request_goal(file_summary, goal="Summarize the changes to the files.")}
"""

    if os.getenv("TEST_SUMMARY", "true").lower() == "true":
        summary += f"""
## Test Coverage
{_summarize_test_coverage(git_diff)}
"""

    if os.getenv("BUG_SUMMARY", "true").lower() == "true":
        question = load_bug_yaml().format(diff=git_diff)
        pr_bugs = _ask([question])["response"]

        summary += f"""
## Potential Bugs
{pr_bugs}
"""

    summary += _summarize_risk(git_diff)

    return summary


def _review(diff: str = ".diff", config: str = "config.summary.yml") -> Dict[str, str]:
    """Review a git diff from file

    Args:
        diff (str, optional): The diff to review. Defaults to ".diff".
        config (str, optional): The config to use. Defaults to "config.summary.yml".

    Returns:
        Dict[str, str]: The response from GPT.
    """

    # If config is a file, use it

    with open(diff, "r", encoding="utf8") as file:
        diff_contents = file.read()

        if os.path.isfile(config):
            summary = _process_yaml(git_diff=diff_contents, yaml_file=config)
        else:
            summary = _summarize_files(diff_contents)
        return {"response": summary}


def _process_yaml(git_diff, yaml_file, headers=True) -> str:
    """
    Process a yaml file.

    Args:
        git_diff (str): The diff of the PR.
        yaml_file (str): The path to the yaml file.
        headers (bool, optional): Whether to include headers. Defaults to True.

    Returns:
        str: The report.
    """
    with open(yaml_file, "r", encoding="utf8") as file:
        yaml_contents = file.read()
        config = yaml.safe_load(yaml_contents)
        report = config["report"]

        return _process_report(git_diff, report, headers=headers)


def _process_report(git_diff, report: dict, indent="#", headers=True) -> str:
    """
    for-each record in report
    - if record is a string, check_goals
    - else recursively call process_report

    Args:
        git_diff (str): The diff of the PR.
        report (dict): The report to process.
        indent (str, optional): The indent to use. Defaults to "#".
        headers (bool, optional): Whether to include headers. Defaults to True.

    Returns:
        str: The report.
    """
    text = ""
    for key, record in report.items():
        if isinstance(record, str) or record is None:
            if headers and key != "_":
                text += f"""
{indent} {key}
"""
            text += f"{_request_goal(git_diff, goal=record)}"

        else:
            text += f"""
{indent} {key}
"""
            text += _process_report(git_diff, record, indent=f"{indent}#", headers=headers)

    return text


class ReviewCommandGroup(GPTCommandGroup):
    """Review Command Group."""

    @staticmethod
    def load_command_table(loader: CLICommandsLoader) -> None:
        with CommandGroup(loader, "review", "gpt_review._review#{}", is_preview=True) as group:
            group.command("diff", "_review", is_preview=True)

    @staticmethod
    def load_arguments(loader: CLICommandsLoader) -> None:
        """Add patch_repo, patch_pr, and access_token arguments."""
        with ArgumentsContext(loader, "github") as args:
            args.argument(
                "diff",
                type=str,
                help="Git diff to review.",
                default=".diff",
            )
            args.argument(
                "config",
                type=str,
                help="The config file to use to customize review summary.",
                default="config.template.yml",
            )
