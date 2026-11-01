"""Fixture: both modes pass representative synthetic fixtures."""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from extraction.modes import MODE_EXTRACT, MODE_REPORT, extract_mode

def test_both_modes():
    assert extract_mode("extract-while-scanning") == MODE_EXTRACT
    assert extract_mode("report-first") == MODE_REPORT
    assert extract_mode("extract-as-we-go") == MODE_EXTRACT
    assert extract_mode("report") == MODE_REPORT
