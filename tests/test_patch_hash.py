from __future__ import annotations

from llvm_hackme.patch_hash import compute_patch_hash

LIB_PATCH = (
    "diff --git a/llvm/lib/Transforms/InstCombine/foo.cpp "
    "b/llvm/lib/Transforms/InstCombine/foo.cpp\n"
    "index 1234567..89abcde 100644\n"
    "--- a/llvm/lib/Transforms/InstCombine/foo.cpp\n"
    "+++ b/llvm/lib/Transforms/InstCombine/foo.cpp\n"
    "@@ -1,3 +1,4 @@\n"
    " int foo() {\n"
    "+  return 1;\n"
    "   return 0;\n"
    " }\n"
)

CLANG_PATCH = (
    "diff --git a/clang/lib/Sema/SemaExpr.cpp b/clang/lib/Sema/SemaExpr.cpp\n"
    "index 1111111..2222222 100644\n"
    "--- a/clang/lib/Sema/SemaExpr.cpp\n"
    "+++ b/clang/lib/Sema/SemaExpr.cpp\n"
    "@@ -1,2 +1,3 @@\n"
    " void f() {\n"
    "+  int x = 1;\n"
    "   (void)0;\n"
    " }\n"
)

TEST_PATCH = (
    "diff --git a/llvm/test/Transforms/InstCombine/add.ll "
    "b/llvm/test/Transforms/InstCombine/add.ll\n"
    "index 3333333..4444444 100644\n"
    "--- a/llvm/test/Transforms/InstCombine/add.ll\n"
    "+++ b/llvm/test/Transforms/InstCombine/add.ll\n"
    "@@ -1,1 +1,1 @@\n"
    "-define i32 @a() { ret i32 0 }\n"
    "+define i32 @a() { ret i32 1 }\n"
)

UNITTEST_PATCH = (
    "diff --git a/llvm/unittests/Transforms/InstCombineTest.cpp "
    "b/llvm/unittests/Transforms/InstCombineTest.cpp\n"
    "index 5555555..6666666 100644\n"
    "--- a/llvm/unittests/Transforms/InstCombineTest.cpp\n"
    "+++ b/llvm/unittests/Transforms/InstCombineTest.cpp\n"
    "@@ -1,2 +1,3 @@\n"
    " TEST(InstCombine, Foo) {\n"
    "+  EXPECT_EQ(1, 1);\n"
    " }\n"
)

MLIR_PATCH = (
    "diff --git a/mlir/lib/Dialect/Arith/IR/ArithOps.cpp "
    "b/mlir/lib/Dialect/Arith/IR/ArithOps.cpp\n"
    "index 7777777..8888888 100644\n"
    "--- a/mlir/lib/Dialect/Arith/IR/ArithOps.cpp\n"
    "+++ b/mlir/lib/Dialect/Arith/IR/ArithOps.cpp\n"
    "@@ -1,2 +1,3 @@\n"
    " void g() {\n"
    "+  (void)2;\n"
    "   (void)1;\n"
    " }\n"
)


class TestComputePatchHash:
    def test_ignores_non_llvm_changes(self) -> None:
        assert compute_patch_hash(CLANG_PATCH) == compute_patch_hash("")
        assert compute_patch_hash(MLIR_PATCH) == compute_patch_hash("")

    def test_ignores_test_and_unittest_changes(self) -> None:
        assert compute_patch_hash(TEST_PATCH) == compute_patch_hash("")
        assert compute_patch_hash(UNITTEST_PATCH) == compute_patch_hash("")

    def test_hashes_llvm_lib_changes(self) -> None:
        assert compute_patch_hash(LIB_PATCH) != compute_patch_hash("")

    def test_test_updates_do_not_change_hash(self) -> None:
        mixed_a = LIB_PATCH + TEST_PATCH
        mixed_b = LIB_PATCH + TEST_PATCH.replace(
            "+define i32 @a() { ret i32 1 }",
            "+define i32 @a() { ret i32 2 }",
        )
        assert compute_patch_hash(mixed_a) == compute_patch_hash(mixed_b)
        assert compute_patch_hash(mixed_a) == compute_patch_hash(LIB_PATCH)

    def test_lib_change_changes_hash(self) -> None:
        other = LIB_PATCH.replace("+  return 1;", "+  return 2;")
        assert compute_patch_hash(LIB_PATCH) != compute_patch_hash(other)

    def test_deterministic(self) -> None:
        assert compute_patch_hash(LIB_PATCH + TEST_PATCH) == compute_patch_hash(
            LIB_PATCH + TEST_PATCH
        )

    def test_binary_payload_is_hashed(self) -> None:
        binary_a = (
            "diff --git a/llvm/lib/Support/foo.bin b/llvm/lib/Support/foo.bin\n"
            "new file mode 100644\n"
            "index 0000000..3b18e51\n"
            "GIT binary patch\n"
            "literal 4\n"
            "Kc$!U:^$!Q\n"
            "\n"
        )
        binary_b = binary_a.replace("..3b18e51", "..deadbeef").replace(
            "literal 4", "literal 8"
        )
        assert compute_patch_hash(binary_a) != compute_patch_hash(binary_b)
        assert compute_patch_hash(binary_a) != compute_patch_hash("")

    def test_rename_into_llvm_counts(self) -> None:
        rename = (
            "diff --git a/clang/lib/Sema/x.cpp "
            "b/llvm/lib/Transforms/InstCombine/x.cpp\n"
            "similarity index 90%\n"
            "rename from clang/lib/Sema/x.cpp\n"
            "rename to llvm/lib/Transforms/InstCombine/x.cpp\n"
            "index 2222222..3333333 100644\n"
            "--- a/clang/lib/Sema/x.cpp\n"
            "+++ b/llvm/lib/Transforms/InstCombine/x.cpp\n"
            "@@ -1,2 +1,3 @@\n"
            " void h() {\n"
            "+  (void)3;\n"
            "   (void)1;\n"
            " }\n"
        )
        assert compute_patch_hash(rename) != compute_patch_hash("")
