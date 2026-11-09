import re

from mvoi.deploy import semantic


def test_relationships_reference_existing_columns_with_matching_types():
    cols = {(t.name, c.name): c for t in semantic.TABLES for c in t.cols}
    for ft, fc, tt, tc in semantic.RELATIONSHIPS:
        assert (ft, fc) in cols and (tt, tc) in cols, (ft, fc, tt, tc)
        assert cols[(ft, fc)].dtype == cols[(tt, tc)].dtype
        assert cols[(ft, fc)].hidden, f"foreign key {ft}.{fc} should be hidden"
        assert cols[(tt, tc)].is_key


def test_sort_by_and_measures():
    for t in semantic.TABLES:
        names = {c.name for c in t.cols}
        for c in t.cols:
            if c.sort_by:
                assert c.sort_by in names
        for m in t.measures:
            assert m.fmt and m.desc and m.folder
            measures = {x.name for tt in semantic.TABLES for x in tt.measures}
            for tbl, col in re.findall(r"'?([\w ]*?)'?\[([^\]]+)\]", m.dax):
                tname = tbl.strip()
                if not tname:
                    assert col in measures, (m.name, col)
                    continue
                assert any(x.name == tname and col in {c.name for c in x.cols} for x in semantic.TABLES), \
                    (m.name, tname, col)


def test_every_fact_is_related_except_map_table():
    related = {r[0] for r in semantic.RELATIONSHIPS} | {r[2] for r in semantic.RELATIONSHIPS}
    unrelated = {t.name for t in semantic.TABLES} - related
    assert unrelated == {"Hot Spots"}


def test_render_uses_tabs_and_directlake_partitions():
    files = semantic.render()
    for path, text in files.items():
        if path.endswith(".tmdl") and "/tables/" in path:
            assert "mode: directLake" in text and "= entity" in text
            assert not re.search(r"^ +\S", text, flags=re.M), f"space-indented line in {path}"
    assert "{{LAKEHOUSE_ID}}" in files["definition/expressions.tmdl"]
    assert "discourageImplicitMeasures" in files["definition/model.tmdl"]
