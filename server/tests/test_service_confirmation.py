from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.service_check import ServiceCheckCreate, ServiceCheckUpdate
from app.schemas.sensor import ServiceResultItem, ConfigServiceCheck


def test_new_service_defaults_and_explicit_existing_settings():
    new = ServiceCheckCreate(name="Health", check_type="http", target_host="example.test")
    assert (new.check_interval, new.timeout, new.retry_count, new.retry_delay_s, new.down_check_interval) == (60, 10, 3, 10, 30)
    existing = ServiceCheckUpdate(retry_count=5, retry_delay_s=17, down_check_interval=45)
    assert existing.model_dump(exclude_unset=True) == {"retry_count": 5, "retry_delay_s": 17, "down_check_interval": 45}
    assert ServiceCheckUpdate(name="Renamed").model_dump(exclude_unset=True) == {"name": "Renamed"}


def test_sensor_protocol_preserves_legacy_final_results_and_new_progress():
    args = dict(service_check_id=uuid4(), timestamp=datetime.now(timezone.utc), check_type="http", is_up=False)
    old = ServiceResultItem(**args)
    assert old.confirmation_complete and old.confirmation_attempt == 0
    partial = ServiceResultItem(**args, confirmation_complete=False, confirmation_attempt=1)
    assert not partial.confirmation_complete
    assert ConfigServiceCheck(id=str(uuid4()), name="Health", check_type="http").confirmation_progress
