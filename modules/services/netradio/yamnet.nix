# YAMNet (Google's AudioSet classifier, 521 classes) as ONNX — a tf2onnx
# conversion mirrored on Hugging Face, pinned to a commit. ~16 MB, fetched at
# build time. The classifier the profiler listens with, on the server and on the
# the compute box profile server alike (one derivation, imported by both).
{ fetchurl }:
fetchurl {
  url = "https://huggingface.co/andrelgomes/yamnet-onnx/resolve/8a03a1572569685c42fdbef54ff36435dbaaf689/yamnet.onnx";
  hash = "sha256-FRAEHc4kounoTsVGgHrECK5JbabR7UG8PMumSWI/jhk=";
}
