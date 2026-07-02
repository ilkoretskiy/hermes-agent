# Hermes Skill Layout and Authoring Guide

This guide explains how to organize Hermes skills so agents can discover,
understand, and load them reliably. It focuses on folder layout, `SKILL.md`
frontmatter, category descriptions, and practical examples.

## Short Version

Use this layout by default:

```text
skills/<category>/<skill-name>/SKILL.md
```

Example:

```text
skills/mlops/evaluation/deepeval/SKILL.md
```

Keep the final folder name and the frontmatter `name` the same:

```yaml
---
name: deepeval
description: Evaluate LLM outputs with DeepEval.
---
```

Use `DESCRIPTION.md` to explain categories:

```text
skills/mlops/evaluation/DESCRIPTION.md
```

```md
---
description: Model evaluation benchmarks, experiment tracking, and interpretability tools.
---
```

Avoid adding extra explanatory folders just to describe a category. Prefer a
clear category path plus `DESCRIPTION.md`.

## How Hermes Finds Skills

Hermes uses two related but different mechanisms:

1. The system prompt skill index shows available skills to the model.
2. `skill_view(name)` loads the full content of a chosen skill.

These mechanisms do not resolve names in exactly the same way.

### System Prompt Index

At runtime, Hermes builds an `<available_skills>` block in the system prompt.
This block includes:

- category path, derived from folders above the skill directory
- optional category description, read from `DESCRIPTION.md`
- skill name, read from `SKILL.md` frontmatter `name`
- skill description, read from `SKILL.md` frontmatter `description`

For this structure:

```text
skills/mlops/evaluation/DESCRIPTION.md
skills/mlops/evaluation/deepeval/SKILL.md
```

Hermes presents something like:

```text
<available_skills>
  mlops/evaluation: Model evaluation benchmarks, experiment tracking, and interpretability tools.
    - deepeval: Evaluate LLM outputs with DeepEval.
</available_skills>
```

This helps the model decide which skill is relevant before loading the full
`SKILL.md`.

### `skill_view(name)` Resolution

For local skills, `skill_view(name)` searches mainly by filesystem layout:

1. Direct relative path, such as `mlops/evaluation/deepeval`
2. Recursive final folder name, such as `deepeval`
3. Legacy flat markdown file, such as `deepeval.md`

This means the final folder name is important. If the frontmatter says
`name: deepeval` but the folder is named `llm-eval-helper`, the model may see
`deepeval` in the skills index and try:

```text
skill_view("deepeval")
```

That can fail unless there is a matching folder/path. Avoid this by keeping the
frontmatter `name` and final folder name identical.

## Recommended Layouts

### Simple Category

Use this when a skill belongs to a broad area:

```text
skills/github/pr-review/SKILL.md
```

```yaml
---
name: pr-review
description: Review GitHub pull requests for regressions.
---
```

Good because:

- `skill_view("pr-review")` can resolve by folder name
- category shown to the model is `github`
- folder structure is easy to scan

### Category With Description

Use `DESCRIPTION.md` when the category needs explanation:

```text
skills/github/DESCRIPTION.md
skills/github/pr-review/SKILL.md
skills/github/fix-ci/SKILL.md
```

```md
---
description: GitHub workflow skills for pull requests, reviews, issues, and CI.
---
```

The category header becomes more informative:

```text
github: GitHub workflow skills for pull requests, reviews, issues, and CI.
  - fix-ci: Debug failing GitHub Actions checks.
  - pr-review: Review GitHub pull requests for regressions.
```

### Meaningful Subcategory

Use a subcategory when it helps users and models distinguish related groups:

```text
skills/mlops/evaluation/DESCRIPTION.md
skills/mlops/evaluation/deepeval/SKILL.md
skills/mlops/evaluation/lm-eval/SKILL.md
skills/mlops/training/axolotl/SKILL.md
```

This is useful because `mlops/evaluation` and `mlops/training` are genuinely
different task areas.

### External or Larger Skill Sets

For large libraries, deeper categories can be reasonable:

```text
skills/data-science/visualization/plotly/SKILL.md
skills/data-science/visualization/matplotlib/SKILL.md
skills/data-science/statistics/hypothesis-testing/SKILL.md
```

This is acceptable when each level carries real meaning.

## Layouts to Avoid

### Extra Explanatory Folder

Avoid:

```text
skills/category/internal-dir-name/folder-explaining-category/nice-display-name/SKILL.md
```

Prefer:

```text
skills/category/internal-dir-name/DESCRIPTION.md
skills/category/internal-dir-name/nice-display-name/SKILL.md
```

Reason: the category explanation belongs in `DESCRIPTION.md`, not in another
folder level. Extra folder levels make the category path longer without making
`skill_view("nice-display-name")` resolve any better.

### Mismatched Folder and Frontmatter Name

Avoid:

```text
skills/github/pr-reviewer/SKILL.md
```

```yaml
---
name: github-review
description: Review GitHub pull requests.
---
```

The model sees `github-review`, but the folder resolver can find `pr-reviewer`.
That mismatch makes skill loading less reliable.

Prefer:

```text
skills/github/github-review/SKILL.md
```

```yaml
---
name: github-review
description: Review GitHub pull requests.
---
```

### Duplicate Final Folder Names

Avoid:

```text
skills/github/review/SKILL.md
skills/code-review/review/SKILL.md
```

This can make `skill_view("review")` ambiguous. Prefer globally unique skill
folder names:

```text
skills/github/github-review/SKILL.md
skills/code-review/python-code-review/SKILL.md
```

If duplicate names are unavoidable, call them by full relative path:

```text
skill_view("github/review")
skill_view("code-review/review")
```

## `DESCRIPTION.md`

`DESCRIPTION.md` is a category metadata file. It does not define a skill and it
is not loaded by `skill_view()`.

It exists to make category headers in `<available_skills>` more useful.

Use this format:

```md
---
description: Short explanation of this category.
---
```

Place it in the folder it describes:

```text
skills/mlops/DESCRIPTION.md
skills/mlops/evaluation/DESCRIPTION.md
skills/productivity/ocr-and-documents/DESCRIPTION.md
```

For the main system prompt pipeline, use the YAML frontmatter `description`
field. Some helper code can fall back to body text, but frontmatter is the
reliable format for the agent prompt.

Good `DESCRIPTION.md` examples:

```md
---
description: Model evaluation benchmarks, experiment tracking, data curation, tokenizers, and interpretability tools.
---
```

```md
---
description: GitHub workflow skills for repositories, pull requests, issues, reviews, and CI.
---
```

Keep category descriptions short. They are inserted into the system prompt, so
they should orient the model without becoming documentation.

## `SKILL.md` Frontmatter

Every skill needs at least:

```yaml
---
name: skill-name
description: One short sentence explaining when to use it.
---
```

Project standards for this repository are stricter:

- `description` should be 60 characters or less
- `description` should be one sentence
- `description` should end with a period
- avoid marketing words like "powerful", "comprehensive", "seamless", and
  "advanced"
- do not repeat the skill name in the description unless needed for clarity

Good:

```yaml
---
name: fix-github-ci
description: Debug failing GitHub Actions checks.
---
```

Weak:

```yaml
---
name: fix-github-ci
description: A powerful and comprehensive skill for fixing GitHub CI problems.
---
```

## Supporting Files

Keep `SKILL.md` focused on the workflow. Put supporting material in predictable
folders:

```text
skill-name/
├── SKILL.md
├── scripts/
├── references/
├── templates/
└── assets/
```

Use:

- `scripts/` for deterministic helper code
- `references/` for detailed documentation the agent may load when needed
- `templates/` for reusable output templates
- `assets/` for files used as resources, such as images or boilerplate

Do not bury important procedural instructions several files deep. `SKILL.md`
should clearly say which supporting file to read or run and when.

## Good Complete Example

```text
skills/mlops/evaluation/DESCRIPTION.md
skills/mlops/evaluation/deepeval/SKILL.md
skills/mlops/evaluation/deepeval/references/assertions.md
skills/mlops/evaluation/deepeval/scripts/summarize_results.py
```

`skills/mlops/evaluation/DESCRIPTION.md`:

```md
---
description: Model evaluation benchmarks, experiment tracking, and interpretability tools.
---
```

`skills/mlops/evaluation/deepeval/SKILL.md`:

```md
---
name: deepeval
description: Evaluate LLM outputs with DeepEval.
---

# DeepEval Skill

Use this skill when evaluating LLM outputs with DeepEval.

## When to Use

- The user asks to write or run DeepEval tests.
- The user needs assertions for LLM output quality.
- The user needs to summarize evaluation results.

## Procedure

1. Inspect the target outputs and expected criteria.
2. Choose the appropriate DeepEval metric.
3. Read `references/assertions.md` if custom assertions are needed.
4. Run `scripts/summarize_results.py` after evaluation if a summary is needed.

## Verification

Confirm the evaluation command ran and report the result file path.
```

Why this works:

- `deepeval` is both the folder name and frontmatter name
- category path `mlops/evaluation` is meaningful
- `DESCRIPTION.md` explains the category
- detailed material is split into `references/` and `scripts/`

## Decision Rules

Use `skills/<category>/<skill-name>/SKILL.md` when:

- there are only a few skills in the category
- the category name is already clear
- the skill name is globally unique

Use `skills/<category>/<subcategory>/<skill-name>/SKILL.md` when:

- the subcategory has several related skills
- the subcategory name helps the model choose the right skill
- the path remains easy to scan

Use `DESCRIPTION.md` when:

- the category name is abbreviated, internal, or ambiguous
- several skills share the category
- you are tempted to add an explanatory folder

Rename the skill folder when:

- it does not match frontmatter `name`
- it is too generic, such as `review`, `test`, or `deploy`
- another skill already uses the same final folder name

## Review Checklist

Before sharing or merging a skill, check:

- final folder name matches `SKILL.md` frontmatter `name`
- final folder name is globally unique where possible
- category path is meaningful and not overly deep
- category explanation is in `DESCRIPTION.md`, not in a filler folder
- `DESCRIPTION.md` uses YAML frontmatter `description`
- `SKILL.md` description is short, specific, and trigger-oriented
- supporting files live in `scripts/`, `references/`, `templates/`, or `assets/`
- `SKILL.md` tells the agent when to read or run supporting files
- no extra README, changelog, or process notes are included inside the skill

## Practical Recommendation

Default to this:

```text
skills/<category>/<skill-name>/SKILL.md
skills/<category>/DESCRIPTION.md
```

Only add deeper category levels when the extra level represents a real, reusable
classification. Do not use folder depth as documentation. Use `DESCRIPTION.md`
for that.
