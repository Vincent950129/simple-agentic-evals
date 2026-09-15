# Tutorials

All notebooks use the served `simple-agentic-evals` wheel and send the eval key
when requesting `/sdk`.

| notebook | use it for |
| --- | --- |
| `evolve_eval_colab_tutorial_quick_start.ipynb` | shortest hosted evaluation walkthrough |
| `evolve_eval_colab_tutorial_quick_start_detail.ipynb` | quick start with more explanation |
| `evolve_eval_colab_tutorial_full.ipynb` | complete EOG/ALE workflow, task metadata, verifier detail, scoring |
| `evolve_eval_colab_tutorial_evovle_only.ipynb` | evolving-resource workflow |
| `eval_service_demo_eog.ipynb` | focused EOG service demonstration |
| `eval_service_demo_eog_ale.ipynb` | compact EOG and ALE demonstration |

Set `EVAL_SERVICE_URL`, `EVAL_SERVICE_API_KEY`, and—when using a provided model
harness—`OPENAI_API_KEY` in the environment or notebook secrets. The eval key
authenticates service calls; the OpenAI key pays for and authenticates model
inference. They are not interchangeable.

The root `evolve_eval_colab_tutorial.ipynb` is kept as a compatibility copy of
the full tutorial.
