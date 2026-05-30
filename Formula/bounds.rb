# bounds.rb — Homebrew formula for the Bounds CLI
# ------------------------------------------------
# This is a TAP formula, intended to live in a `homebrew-bounds` tap
# (i.e. `brew install Farzin312/bounds/bounds`), PENDING the PyPI publish of
# the `bounds` package. It is NOT yet submitted to homebrew-core.
#
# Before this formula works you MUST fill in the real release coordinates:
#   1. Publish `bounds` to PyPI.
#   2. Set `url` + `sha256` to the published sdist (see TODOs below).
#   3. Regenerate the dependency `resource` blocks with:
#         brew update-python-resources Formula/bounds.rb
#      That command reads the deps (click, PyYAML, tree-sitter,
#      tree-sitter-python, tree-sitter-typescript) and writes pinned
#      `resource` stanzas with their own url/sha256. We intentionally do NOT
#      hand-fabricate those hashes here.
#
# Honesty note: the url/sha256 below are PLACEHOLDERS and will not install
# until replaced with real PyPI values.

class Bounds < Formula
  include Language::Python::Virtualenv

  desc "Architecture contract enforcer — zero-LLM structural validation via tree-sitter"
  homepage "https://github.com/Farzin312/bounds"
  # TODO: fill from PyPI release — e.g.
  #   https://files.pythonhosted.org/packages/source/b/bounds/bounds-0.1.0.tar.gz
  url "https://files.pythonhosted.org/packages/source/b/bounds/bounds-0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000" # TODO: fill from PyPI release
  license "MIT"

  depends_on "python@3.12"

  # TODO: run `brew update-python-resources Formula/bounds.rb` to populate the
  # resource blocks for the runtime dependencies. The package depends on:
  #   click >=8.1
  #   PyYAML >=6
  #   tree-sitter >=0.21,<0.26
  #   tree-sitter-python >=0.21
  #   tree-sitter-typescript >=0.21
  #
  # Until then, the resources below are PLACEHOLDERS and the formula will not
  # build. Replace each with the auto-generated stanza.
  #
  # resource "click" do
  #   url "https://files.pythonhosted.org/packages/source/c/click/click-8.1.7.tar.gz"
  #   sha256 "0000000000000000000000000000000000000000000000000000000000000000" # TODO
  # end
  #
  # resource "PyYAML" do
  #   url "https://files.pythonhosted.org/packages/source/P/PyYAML/PyYAML-6.0.2.tar.gz"
  #   sha256 "0000000000000000000000000000000000000000000000000000000000000000" # TODO
  # end
  #
  # (...and resources for tree-sitter, tree-sitter-python, tree-sitter-typescript)

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "Bounds", shell_output("#{bin}/bounds --help")
  end
end
