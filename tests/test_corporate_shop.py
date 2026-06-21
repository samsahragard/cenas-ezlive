from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services import corporate_shop


def test_list_orders_filters_store_before_limit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    corporate_shop.CorporateBase.metadata.create_all(engine)
    monkeypatch.setattr(corporate_shop, "_engine", engine)
    monkeypatch.setattr(corporate_shop, "_Session", Session)
    monkeypatch.setattr(corporate_shop, "_schema_checked", True)

    with Session() as s:
        tomball = corporate_shop.Customer(
            email=corporate_shop.STORE_CUSTOMER_EMAIL["tomball"],
            username="Tomball Kitchen",
        )
        copperfield = corporate_shop.Customer(
            email=corporate_shop.STORE_CUSTOMER_EMAIL["copperfield"],
            username="Copperfield Kitchen",
        )
        s.add_all([tomball, copperfield])
        s.flush()
        s.add_all([
            corporate_shop.Order(customer_link=tomball.id, status="Submitted"),
            corporate_shop.Order(customer_link=copperfield.id, status="Submitted"),
        ])
        s.commit()

    rows = corporate_shop.list_orders(limit=25, store_filter="copperfield")

    assert len(rows) == 1
    assert rows[0]["customer_email"] == corporate_shop.STORE_CUSTOMER_EMAIL["copperfield"]
