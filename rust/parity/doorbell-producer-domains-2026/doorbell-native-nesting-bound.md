| Named substitution | Producer and supported domain | Refusal and controls |
| --- | --- | --- |
| `doorbell-native-nesting-bound` | Fixed pending records are shallow; arbitrary hook metadata is admitted under serde's default recursion budget of 128. Windows native parser unit checks accept 127 nested arrays and refuse 128 and 2000. The source-derived budget also counts object containers. | The same quiet stdin/pending refusal rules apply. Whole-input boundary controls counting the root object remain unexecuted. No CPython recursion frame accounting or extra parser thread remains. |
