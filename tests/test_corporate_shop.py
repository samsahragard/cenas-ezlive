from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.services import corporate_shop


def test_corporate_picture_url_handles_legacy_media_filenames():
    assert (
        corporate_shop._picture_url("Amareto_Torani_Syrup.jpg")
        == "https://cenaskitchen.com/media/Amareto_Torani_Syrup.jpg"
    )
    assert (
        corporate_shop._picture_url("/media/Amareto_Torani_Syrup.jpg")
        == "https://cenaskitchen.com/media/Amareto_Torani_Syrup.jpg"
    )
    assert (
        corporate_shop._picture_url("https://example.com/pic.webp")
        == "https://example.com/pic.webp"
    )


def test_corporate_catalog_merges_takeout_departments():
    seed = corporate_shop.load_catalog_seed()
    legacy = {
        "1-3 Compartment Containers",
        "Aluminum Foil Pans & Containers",
        "Togo & Catering",
    }
    category_labels = {row["label"] for row in seed["categories"]}
    merged_items = [
        item for item in seed["items"]
        if item["category"] == "Take-out & Catering"
    ]

    assert "Take-out & Catering" in category_labels
    assert category_labels.isdisjoint(legacy)
    assert len(merged_items) == 34
    assert {item["category_key"] for item in merged_items} == {"takeout_catering"}
    assert all(item["category"] not in legacy for item in seed["items"])


def test_corporate_catalog_merges_cups_and_lids_departments():
    seed = corporate_shop.load_catalog_seed()
    legacy = {
        "Foam Cups and Lids",
        "Portion Cup & Lids",
    }
    category_labels = {row["label"] for row in seed["categories"]}
    merged_items = [
        item for item in seed["items"]
        if item["category"] == "Cups & Lids"
    ]

    assert "Cups & Lids" in category_labels
    assert category_labels.isdisjoint(legacy)
    assert len(merged_items) == 11
    assert {item["category_key"] for item in merged_items} == {"cups_lids"}
    assert all(item["category"] not in legacy for item in seed["items"])


def test_corporate_catalog_merges_host_togo_bar_and_server_into_foh():
    seed = corporate_shop.load_catalog_seed()
    legacy = {
        "Server",
        "Host & Togo",
        "Bar",
    }
    category_labels = {row["label"] for row in seed["categories"]}
    merged_items = [
        item for item in seed["items"]
        if item["category"] == "FOH"
    ]

    assert "FOH" in category_labels
    assert category_labels.isdisjoint(legacy)
    assert len(merged_items) == 34
    assert {item["category_key"] for item in merged_items} == {"foh"}
    assert all(item["category"] not in legacy for item in seed["items"])


def test_corporate_catalog_merges_office_and_uniforms():
    seed = corporate_shop.load_catalog_seed()
    legacy = {
        "Office",
        "Uniforms",
    }
    category_labels = {row["label"] for row in seed["categories"]}
    merged_items = [
        item for item in seed["items"]
        if item["category"] == "Office & Uniforms"
    ]

    assert "Office & Uniforms" in category_labels
    assert category_labels.isdisjoint(legacy)
    assert len(merged_items) == 15
    assert {item["category_key"] for item in merged_items} == {"office_uniforms"}
    assert all(item["category"] not in legacy for item in seed["items"])


def test_corporate_catalog_merges_cleaning_and_spices_into_boh():
    seed = corporate_shop.load_catalog_seed()
    legacy = {
        "Cleaning Supplies",
        "Spices",
    }
    category_labels = {row["label"] for row in seed["categories"]}
    merged_items = [
        item for item in seed["items"]
        if item["category"] == "BOH"
    ]

    assert "BOH" in category_labels
    assert category_labels.isdisjoint(legacy)
    assert len(merged_items) == 31
    assert {item["category_key"] for item in merged_items} == {"boh"}
    assert all(item["category"] not in legacy for item in seed["items"])


def test_corporate_catalog_includes_new_webstaurant_items():
    seed = corporate_shop.load_catalog_seed()
    by_name = {item["name"]: item for item in seed["items"]}
    expected = {
        "Regal Extra Coarse Kosher Salt - 7 lb. (Each)": ("BOH", "each"),
        "Choice Heavy-Duty 24 oz. Translucent Plastic Deli Container and Lid Combo Pack - 240/Case": ("Take-out & Catering", "case"),
        "Choice Heavy-Duty 16 oz. Translucent Plastic Deli Container and Lid Combo Pack - 240/Case": ("Take-out & Catering", "case"),
        "Real Blackberry Puree Infused Syrup 16.9 fl. oz. (Each)": ("FOH", "each"),
        "Real Mango Puree Infused Syrup 16.9 fl. oz. (Each)": ("FOH", "each"),
        "Real Watermelon Puree Infused Syrup 16.9 fl. oz. (Each)": ("FOH", "each"),
        "Real Passion Fruit Puree Infused Syrup 16.9 fl. oz. (Each)": ("FOH", "each"),
        "Real Pineapple Puree Infused Syrup 16.9 fl. oz. (Each)": ("FOH", "each"),
        "Real Peach Puree Infused Syrup 16.9 fl. oz. (Each)": ("FOH", "each"),
        "Real Guava Puree Infused Syrup 16.9 fl. oz. (Each)": ("FOH", "each"),
        "Choice 9\" x 7\" Molded Fiber / Pulp Rectangular Tray - 250/Case": ("Take-out & Catering", "case"),
        "Bigelow Earl Grey Tea Bags - 168/Case": ("FOH", "case"),
    }

    assert len(seed["items"]) == 169
    for name, (category, unit) in expected.items():
        assert name in by_name
        assert by_name[name]["category"] == category
        assert by_name[name]["unit"] == unit
        assert by_name[name]["in_stock"] == 10
        assert by_name[name]["picture"].startswith("https://www.webstaurantstore.com/images/")


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


def test_list_products_uses_custom_sort_order(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    corporate_shop.CorporateBase.metadata.create_all(engine)
    monkeypatch.setattr(corporate_shop, "_engine", engine)
    monkeypatch.setattr(corporate_shop, "_Session", Session)
    monkeypatch.setattr(corporate_shop, "_schema_checked", True)

    with Session() as s:
        s.add_all([
            corporate_shop.Product(
                product_name="Second",
                in_stock=1,
                product_picture="",
                category="FOH",
                sort_order=20,
            ),
            corporate_shop.Product(
                product_name="First",
                in_stock=1,
                product_picture="",
                category="FOH",
                sort_order=10,
            ),
        ])
        s.commit()

    rows = corporate_shop.list_products(category="FOH")

    assert [row["name"] for row in rows] == ["First", "Second"]


def test_update_product_order_persists_department_order(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    corporate_shop.CorporateBase.metadata.create_all(engine)
    monkeypatch.setattr(corporate_shop, "_engine", engine)
    monkeypatch.setattr(corporate_shop, "_Session", Session)
    monkeypatch.setattr(corporate_shop, "_schema_checked", True)

    with Session() as s:
        s.add_all([
            corporate_shop.Product(
                id=1,
                product_name="A",
                in_stock=1,
                product_picture="",
                category="FOH",
                sort_order=10,
            ),
            corporate_shop.Product(
                id=2,
                product_name="B",
                in_stock=1,
                product_picture="",
                category="FOH",
                sort_order=20,
            ),
            corporate_shop.Product(
                id=3,
                product_name="C",
                in_stock=1,
                product_picture="",
                category="FOH",
                sort_order=30,
            ),
            corporate_shop.Product(
                id=4,
                product_name="Salt",
                in_stock=1,
                product_picture="",
                category="BOH",
                sort_order=10,
            ),
        ])
        s.commit()

    count = corporate_shop.update_product_order("FOH", [3, 1, 2])
    foh_rows = corporate_shop.list_products(category="FOH")
    boh_rows = corporate_shop.list_products(category="BOH")

    assert count == 3
    assert [row["name"] for row in foh_rows] == ["C", "A", "B"]
    assert [row["name"] for row in boh_rows] == ["Salt"]


def test_adjust_stock_adds_delta_and_clamps_at_zero(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    corporate_shop.CorporateBase.metadata.create_all(engine)
    monkeypatch.setattr(corporate_shop, "_engine", engine)
    monkeypatch.setattr(corporate_shop, "_Session", Session)
    monkeypatch.setattr(corporate_shop, "_schema_checked", True)

    with Session() as s:
        s.add(corporate_shop.Product(
            id=1,
            product_name="Bleach",
            in_stock=5,
            product_picture="",
            category="BOH",
            sort_order=10,
        ))
        s.commit()

    assert corporate_shop.adjust_stock(1, 3) == 8
    assert corporate_shop.adjust_stock(1, -99) == 0
    assert corporate_shop.adjust_stock(999, 1) is None
