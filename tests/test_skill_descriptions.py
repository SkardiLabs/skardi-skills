"""Every SKILL.md description stays within the Agent Skills limit.

The Agent Skills specification caps `description` at 1024 characters, and
Anthropic's own skill validator rejects anything longer. A host is free to
handle an over-long description however it likes, and at least one does not
truncate it: in Claude Code, plugin 0.4.0's auto-context (1966 characters) and
graph-rag (2476) were listed with an empty description, while retrieval (1517)
and graph-source (726) were listed in full (observed 2026-11-24). An agent
picks a skill from its description alone, so an empty one means the skill is
practically never selected on its own.

Keep the description to what the skill does, what a user says when they need
it, and what it is not for. Steps the agent takes once the skill is loaded
belong in the body.
"""

import pathlib
import re
import unittest

import yaml

LIMIT = 1024
ROOT = pathlib.Path(__file__).resolve().parent.parent


def frontmatter(path):
    match = re.match(r"---\n(.*?)\n---\n", path.read_text(encoding="utf-8"), re.S)
    if match is None:
        raise AssertionError(f"{path} has no frontmatter")
    return yaml.safe_load(match.group(1))


class SkillDescriptionTest(unittest.TestCase):
    def test_descriptions_fit_the_spec_limit(self):
        skills = sorted(ROOT.glob("skills/*/SKILL.md"))
        self.assertTrue(skills, "no SKILL.md found under skills/")
        for path in skills:
            with self.subTest(skill=path.parent.name):
                description = frontmatter(path).get("description") or ""
                self.assertTrue(description.strip(), f"{path} has an empty description")
                self.assertLessEqual(
                    len(description),
                    LIMIT,
                    f"{path.parent.name}: description is {len(description)} characters; "
                    f"the limit is {LIMIT}. Move the steps into the body.",
                )


if __name__ == "__main__":
    unittest.main()
