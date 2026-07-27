import pytest

from photo2video.sources import Range, SourceError, from_directory, parse_range, split_name


class TestSplitName:
    def test_with_ext(self):
        assert split_name("P1001222.JPG") == ("P", 1001222, 7)

    def test_underscore_prefix(self):
        assert split_name("IMG_0001.jpeg") == ("IMG_", 1, 4)

    def test_no_number(self):
        assert split_name("cover.png") == ("cover", -1, 0)


class TestParseRange:
    def test_full(self):
        r = parse_range("P1001222-P1001325")
        assert (r.prefix, r.start, r.end, r.expected) == ("P", 1001222, 1001325, 104)

    def test_with_extensions(self):
        assert parse_range("P1001222.JPG-P1001325.JPG") == Range("P", 1001222, 1001325, 7)

    def test_shorthand_right(self):
        assert parse_range("P1001222-1325") == Range("P", 1001222, 1001325, 7)
        assert parse_range("P1001222-325") == Range("P", 1001222, 1001325, 7)

    def test_matches(self):
        r = parse_range("P1001222-P1001325")
        assert r.matches("P1001222.JPG") and r.matches("P1001325.jpeg")
        assert not r.matches("P1001221.JPG")
        assert not r.matches("Q1001250.JPG")  # 前缀不同

    def test_label_pads(self):
        assert parse_range("IMG_0001-0010").label(7) == "IMG_0007"

    def test_errors(self):
        for bad, msg in [("P1001222", "格式"), ("abc-def", "没有数字"),
                         ("P1001325-P1001222", "小于"), ("P100-Q200", "前缀")]:
            with pytest.raises(SourceError, match=msg):
                parse_range(bad)


class TestFromDirectory:
    def _mk(self, tmp_path, names):
        for n in names:
            (tmp_path / n).write_bytes(b"x")
        return tmp_path

    def test_sorts_numerically_not_lexically(self, tmp_path):
        d = self._mk(tmp_path, ["P998.JPG", "P1000.JPG", "P99.JPG"])
        photos, _ = from_directory(d)
        assert [p.name for p in photos] == ["P99.JPG", "P998.JPG", "P1000.JPG"]

    def test_filters_by_range_and_type(self, tmp_path):
        d = self._mk(tmp_path, ["P1001222.JPG", "P1001223.JPG", "P1001999.JPG",
                                "notes.txt", ".DS_Store"])
        photos, warnings = from_directory(d, parse_range("P1001222-P1001223"))
        assert [p.name for p in photos] == ["P1001222.JPG", "P1001223.JPG"]
        assert not warnings

    def test_warns_on_gaps(self, tmp_path):
        d = self._mk(tmp_path, ["P1001222.JPG", "P1001225.JPG"])
        _, warnings = from_directory(d, parse_range("P1001222-P1001225"))
        assert any("缺少" in w and "P1001223" in w for w in warnings)

    def test_missing_dir(self, tmp_path):
        with pytest.raises(SourceError, match="不存在"):
            from_directory(tmp_path / "nope")

    def test_no_match(self, tmp_path):
        d = self._mk(tmp_path, ["P1001222.JPG"])
        with pytest.raises(SourceError, match="没有匹配"):
            from_directory(d, parse_range("P2000000-P2000001"))
