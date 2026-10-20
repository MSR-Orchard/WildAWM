# Evaluator data

Set `WGS_VARIANT_CALLING_EVAL_DATA_DIR` to an evaluator-local directory containing:

- `chr17.fa`
- `truth_chr17.vcf.gz` or `truth_chr17.vcf`
- `eval_region_confident.bed`

This directory must not be staged on the agent VM or stored in a public repository. The evaluator downloads only the submitted VCF and compares it against these local files. For ALE CLI runs, `awmeval` points the evaluator at the trusted host task-data copy. Other runs that do not provision this directory may stage the same files from the gated task reference after the solve phase and materialize them into a temporary evaluator directory via `evaluate()`.

Missing evaluator data is an evaluator failure, not a valid partial score.
