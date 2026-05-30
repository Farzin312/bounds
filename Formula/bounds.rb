# bounds.rb — Homebrew formula for the Bounds CLI
# ------------------------------------------------
# TAP formula, intended to live in a `homebrew-bounds` tap, i.e.:
#
#     brew install --HEAD Farzin312/bounds/bounds
#
# WORKS TODAY via --HEAD: it builds from `main` and vendors the five runtime
# dependencies below (real, pinned PyPI sdists with verified sha256). No PyPI
# publish of `bounds` itself is required for the --HEAD path.
#
# STABLE (non-HEAD) install is intentionally not wired yet — it needs the
# package published. To enable `brew install bounds`, after publishing to PyPI
# (or cutting a GitHub release tag), add a `url` + `sha256` for the bounds sdist
# above `license`, e.g.:
#
#     url "https://files.pythonhosted.org/packages/source/b/bounds/bounds-X.Y.Z.tar.gz"
#     sha256 "<sha256 of that sdist>"
#
# and refresh the resource pins with:  brew update-python-resources Formula/bounds.rb
# We do NOT fabricate the bounds sdist hash here — a fake hash is worse than an
# honest HEAD-only formula.

class Bounds < Formula
  include Language::Python::Virtualenv

  desc "Architecture contract enforcer — zero-LLM structural validation via tree-sitter"
  homepage "https://github.com/Farzin312/bounds"
  license "MIT"
  head "https://github.com/Farzin312/bounds.git", branch: "main"

  depends_on "python@3.12"

  # Runtime dependencies — real, pinned PyPI sdists (verified sha256).
  # Regenerate with `brew update-python-resources Formula/bounds.rb` when bumping.
  resource "click" do
    url "https://files.pythonhosted.org/packages/9b/98/518d8e5081007684232226f475082b30087d0f585e8457db087298259f49/click-8.4.1.tar.gz"
    sha256 "918b5633eddf6b41c32d4f454bf0de810065c74e3f7dbf8ee5452f8be88d3e96"
  end

  resource "PyYAML" do
    url "https://files.pythonhosted.org/packages/05/8e/961c0007c59b8dd7729d542c61a4d537767a59645b82a0b521206e1e25c2/pyyaml-6.0.3.tar.gz"
    sha256 "d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f"
  end

  resource "tree-sitter" do
    url "https://files.pythonhosted.org/packages/66/7c/0350cfc47faadc0d3cf7d8237a4e34032b3014ddf4a12ded9933e1648b55/tree-sitter-0.25.2.tar.gz"
    sha256 "fe43c158555da46723b28b52e058ad444195afd1db3ca7720c59a254544e9c20"
  end

  resource "tree-sitter-python" do
    url "https://files.pythonhosted.org/packages/b8/8b/c992ff0e768cb6768d5c96234579bf8842b3a633db641455d86dd30d5dac/tree_sitter_python-0.25.0.tar.gz"
    sha256 "b13e090f725f5b9c86aa455a268553c65cadf325471ad5b65cd29cac8a1a68ac"
  end

  resource "tree-sitter-typescript" do
    url "https://files.pythonhosted.org/packages/1e/fc/bb52958f7e399250aee093751e9373a6311cadbe76b6e0d109b853757f35/tree_sitter_typescript-0.23.2.tar.gz"
    sha256 "7b167b5827c882261cb7a50dfa0fb567975f9b315e87ed87ad0a0a3aedb3834d"
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "Bounds", shell_output("#{bin}/bounds --help")
  end
end
