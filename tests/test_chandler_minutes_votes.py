"""Tests for Chandler meeting minutes vote parsing."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_tiers import integration_test


@integration_test
def test_parse_minutes_votes_unanimous():
    """Motion carried unanimously (7-0) should produce no supervisor votes."""
    from scraper.jurisdictions.chandler import parse_minutes_votes

    text = """
The meeting was called to order at 6:00 p.m.

Consent Agenda Motion and Vote
Councilmember Orlando moved to approve the Consent Agenda.
Seconded by Councilmember Poston.
Motion carried unanimously (7-0).

Discussion was held on Item 10.
Councilmember Stewart moved to approve the resolution.
Seconded by Councilmember Encinas.
Motion carried unanimously (7-0).
"""
    result = parse_minutes_votes(text)
    assert len(result["votes"]) == 2, f"Expected 2 votes, got {len(result['votes'])}"
    assert result["votes"][0]["motion_result"] == "Carried Unanimously"
    assert result["votes"][1]["motion_result"] == "Carried Unanimously"
    # Unanimous votes should have no supervisor votes
    assert len(result["votes"][0]["supervisor_votes"]) == 0
    assert len(result["votes"][1]["supervisor_votes"]) == 0


@integration_test
def test_parse_minutes_votes_split():
    """Split vote (4-3) with named dissenters."""
    from scraper.jurisdictions.chandler import parse_minutes_votes

    text = """
Councilmember Ellis moved to approve the fee schedule.
Seconded by Councilmember Stewart.
Motion carried by majority (4-3; Councilmembers Encinas, Orlando, and Poston dissenting).
"""
    result = parse_minutes_votes(text)
    assert len(result["votes"]) == 1
    v = result["votes"][0]
    assert v["motion_result"] == "Carried"
    assert len(v["supervisor_votes"]) == 3
    names = {sv["name"] for sv in v["supervisor_votes"]}
    assert "Angel Encinas" in names
    assert "Matt Orlando" in names
    assert "Jane Poston" in names
    assert all(sv["vote"] == "no" for sv in v["supervisor_votes"])


@integration_test
def test_parse_minutes_votes_split_with_mayor():
    """Split vote including Mayor."""
    from scraper.jurisdictions.chandler import parse_minutes_votes

    text = """
Motion carried by majority (2-5; Mayor Hartke, Councilmembers Encinas, Ellis, Orlando, and Poston dissenting).
"""
    result = parse_minutes_votes(text)
    assert len(result["votes"]) == 1
    v = result["votes"][0]
    assert v["motion_result"] == "Failed"  # 2-5 means failed
    assert len(v["supervisor_votes"]) == 5
    names = {sv["name"] for sv in v["supervisor_votes"]}
    assert "Kevin Hartke" in names
    assert "Angel Encinas" in names
    assert "Christine Ellis" in names
    assert "Matt Orlando" in names
    assert "Jane Poston" in names


@integration_test
def test_parse_minutes_votes_conflict_of_interest():
    """Passed N-0 with a councilmember declaring conflict of interest."""
    from scraper.jurisdictions.chandler import parse_minutes_votes

    text = """
Motion carried unanimously (7-0), with the exception of Item No. 28 which passed 6-0,
Councilmember Poston declaring a conflict of interest.
"""
    result = parse_minutes_votes(text)
    assert len(result["votes"]) >= 1


@integration_test
def test_parse_minutes_votes_no_dissent():
    """No voting section at all — should return empty."""
    from scraper.jurisdictions.chandler import parse_minutes_votes

    text = """
The meeting was called to order.
Staff presented the quarterly report.
The council discussed the item.
There being no further business, the meeting adjourned.
"""
    result = parse_minutes_votes(text)
    assert len(result["votes"]) == 0


@integration_test
def test_parse_minutes_votes_realistic():
    """Realistic excerpt from Chandler minutes."""
    from scraper.jurisdictions.chandler import parse_minutes_votes

    text = """
Meeting Minutes
City Council Regular Meeting
November 7, 2024

Roll Call
Council Attendance
Mayor Kevin Hartke
Vice Mayor OD Harris
Councilmember Angel Encinas
Councilmember Christine Ellis
Councilmember Mark Stewart
Councilmember Matt Orlando
Councilmember Jane Poston

Consent Agenda Motion and Vote
Councilmember Orlando moved to approve the Consent Agenda of the November 7, 2024,
Regular City Council Meeting; Seconded by Councilmember Encinas.
Motion carried unanimously (7-0), with the exception of Item No. 28 which passed 6-0,
Councilmember Poston declaring a conflict of interest.

Item 9 — Fee Schedule Amendment
Councilmember Stewart moved to amend the motion.
Vice Mayor Harris seconded.
Vote: The motion failed by lack of second.

Item 10 — Parks and Rec Fees
Vice Mayor Harris made a motion to not increase fees.
Motion carried by majority (4-3; Councilmembers Encinas, Orlando, and Poston dissenting).
"""
    result = parse_minutes_votes(text)
    assert len(result["votes"]) >= 2
    # Should have unanimous vote
    unanimous = [v for v in result["votes"] if v["motion_result"] == "Carried Unanimously"]
    assert len(unanimous) >= 1
    # Should have split vote with dissenters
    split = [v for v in result["votes"] if "dissenting" in v["vote_text"]]
    assert len(split) >= 1
    assert len(split[0]["supervisor_votes"]) == 7
    # Verify majority inference: Mark Stewart should be "yes"
    mark = [sv for sv in split[0]["supervisor_votes"] if sv["name"] == "Mark Stewart"]
    assert len(mark) == 1 and mark[0]["vote"] == "yes"

@integration_test
def test_parse_minutes_votes_majority_inference():
    """Roll call attendance + split vote should infer majority."""
    from scraper.jurisdictions.chandler import parse_minutes_votes

    text = """Meeting Minutes
City Council Regular Meeting
May 21, 2026

Roll Call
Council Attendance
Mayor Kevin Hartke
Vice Mayor OD Harris
Councilmember Angel Encinas
Councilmember Christine Ellis
Councilmember Mark Stewart
Councilmember Matt Orlando
Councilmember Jane Poston

Item 1
Councilmember Stewart moved to approve the item.
Seconded by Councilmember Ellis.
Motion carried by majority (4-3; Councilmembers Encinas, Orlando, and Poston dissenting).
"""
    result = parse_minutes_votes(text)
    supervisors = [s['name'] for s in result['supervisors']]
    assert 'Mark Stewart' in supervisors, "Mark Stewart should be in attendance"
    assert 'Kevin Hartke' in supervisors, "Mayor should be in attendance"

    split = [v for v in result['votes'] if 'dissenting' in v['vote_text']]
    assert len(split) == 1
    svs = split[0]['supervisor_votes']
    
    # Check all 7 members have votes
    assert len(svs) == 7
    
    # Check dissenters voted no
    dissenters = [sv for sv in svs if sv['name'] in ('Angel Encinas', 'Matt Orlando', 'Jane Poston')]
    assert all(sv['vote'] == 'no' for sv in dissenters), "Dissenters should be 'no'"
    
    # Check majority voted yes
    majority = [sv for sv in svs if sv['name'] in ('Kevin Hartke', 'OD Harris', 'Christine Ellis', 'Mark Stewart')]
    assert all(sv['vote'] == 'yes' for sv in majority), "Majority should be 'yes'"
    
    # Verify Mark Stewart specifically
    mark = [sv for sv in svs if sv['name'] == 'Mark Stewart']
    assert len(mark) == 1 and mark[0]['vote'] == 'yes'
