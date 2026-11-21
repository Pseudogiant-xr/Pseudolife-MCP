"""Compatibility launcher for the frozen Python gate and overnight scripts."""
import replicate


def run_one(dataset, extractor, tag):
    from _python_longmemeval_bench import report, run_answer

    run_answer(dataset, extractor, tag)
    report(dataset, extractor, tag)


if __name__ == "__main__":
    raise SystemExit(replicate.main(run_one=run_one))
