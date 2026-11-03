from pathlib import Path
import sys
import pytest

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'appliance/ha'))
from analytics_health import validate,AnalyticsUnavailable


def replica():
    return dict(table='samples',is_readonly=0,is_session_expired=0,active_replicas=2,
                queue_size=0,log_pointer=8,log_max_index=7,lost_part_count=0)


@pytest.mark.parametrize('field,value',[
    ('is_readonly',1),('is_session_expired',1),('active_replicas',1),('lost_part_count',1)])
def test_reject_unavailable_or_lost_copy_even_for_existing_writer(field,value):
    row=replica();row[field]=value
    for caught_up in (True,False):
        with pytest.raises(AnalyticsUnavailable):validate([row],caught_up=caught_up)


@pytest.mark.parametrize('change',[{'queue_size':1},{'log_pointer':7}])
def test_stale_copy_cannot_take_over_but_normal_replication_does_not_stop_writer(change):
    row=replica();row.update(change)
    with pytest.raises(AnalyticsUnavailable):validate([row],caught_up=True)
    validate([row],caught_up=False)


def test_empty_store_refused_and_two_caught_up_copies_admitted():
    with pytest.raises(AnalyticsUnavailable):validate([],caught_up=True)
    validate([replica()],caught_up=True)


def test_zero_log_index_requires_verified_absence_of_first_entry():
    row=replica();row.update(log_pointer=0,log_max_index=0)
    with pytest.raises(AnalyticsUnavailable):validate([row],caught_up=True)
    row['verified_empty_log']=True
    validate([row],caught_up=True)
