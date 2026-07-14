# ADR-011: Cross-volume quarantine and recovery

Status: accepted

Cross-volume moves snapshot and hash the source, copy to a unique partial target,
flush and hash-verify, atomically finalize where supported, then rename the source
into a journal-specific quarantine on its original volume. Quarantine is retained
indefinitely until a later explicit Cleanup review. Incomplete recovery blocks all
new commits.
