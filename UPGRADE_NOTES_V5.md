# OpsPilot v5 upgrade notes

## Prompt privacy test fix

Updated the LLM diagnostic prompt to explain why ranked root-cause hypotheses are omitted without using benchmark-specific terminology. This preserves the prompt's diagnostic guidance and satisfies `test_prompt_contains_no_ground_truth`.

## Verification

- `python3 -m unittest tests.test_opspilot.LLMRepair.test_prompt_contains_no_ground_truth -v`: passed.
- `python3 -m py_compile opspilot/*.py`: passed.
- A full `python3 -m unittest discover -s tests -v` run did not complete within the available verification window, so the full suite is not claimed as passing.
