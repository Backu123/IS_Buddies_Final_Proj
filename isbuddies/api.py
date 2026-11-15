"""
API Blueprint – JSON endpoints for AJAX calls from the frontend.
Handles: Inventory CRUD, Sales transactions, Knowledge Analysis, User management.
"""
import json
import csv
import io
import math
import re
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from flask import Blueprint, abort, jsonify, make_response, request
from flask_login import current_user, login_required
from sqlalchemy import func, desc, or_
from sqlalchemy.exc import IntegrityError

from . import db
from .models import (
    AuditLog, Customer, Product, PurchaseOrder, PurchaseOrderItem, Sale,
    SaleItem, SaleReturn, SaleReturnItem, StockLog, Supplier, PredictionLog, User,
)

bp = Blueprint("api", __name__, url_prefix="/api")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class APIInputError(ValueError):
    pass


@bp.errorhandler(APIInputError)
def handle_input_error(error):
    return jsonify({"error": str(error)}), 400


@bp.errorhandler(403)
def handle_forbidden(error):
    return jsonify({"error": "You do not have permission to perform this action."}), 403


@bp.errorhandler(404)
def handle_not_found(error):
    return jsonify({"error": "The requested record was not found."}), 404


def json_object():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise APIInputError("Request body must be a JSON object.")
    return data


def required_text(data, key, label=None, max_length=120):
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise APIInputError(f"{label or key.replace('_', ' ').capitalize()} is required.")
    value = value.strip()
    if len(value) > max_length:
        raise APIInputError(f"{label or key.replace('_', ' ').capitalize()} must be {max_length} characters or fewer.")
    return value


def optional_text(data, key, max_length=255):
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise APIInputError(f"{key.replace('_', ' ').capitalize()} must be text.")
    value = value.strip()
    if len(value) > max_length:
        raise APIInputError(f"{key.replace('_', ' ').capitalize()} must be {max_length} characters or fewer.")
    return value or None


def optional_email(data, key="email"):
    value = optional_text(data, key, 120)
    if value and not EMAIL_PATTERN.fullmatch(value):
        raise APIInputError("Enter a valid email address.")
    return value


def integer_value(value, label, minimum=0, maximum=2147483647):
    if isinstance(value, bool):
        raise APIInputError(f"{label} must be a whole number.")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        raise APIInputError(f"{label} must be a whole number.") from None
    if str(value).strip() != str(parsed) and not isinstance(value, int):
        raise APIInputError(f"{label} must be a whole number.")
    if parsed < minimum:
        raise APIInputError(f"{label} must be at least {minimum}.")
    if parsed > maximum:
        raise APIInputError(f"{label} cannot exceed {maximum}.")
    return parsed


def money_value(value, label, minimum=Decimal("0")):
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise APIInputError(f"{label} must be a valid amount.") from None
    if not amount.is_finite() or amount < minimum or amount > Decimal("99999999.99"):
        raise APIInputError(f"{label} must be between {minimum} and 99,999,999.99.")
    rounded = amount.quantize(Decimal("0.01"))
    if amount != rounded:
        raise APIInputError(f"{label} cannot have more than two decimal places.")
    return rounded


def finite_number(value, label, minimum=0, maximum=1000000000):
    if isinstance(value, bool):
        raise APIInputError(f"{label} must be a number.")
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        raise APIInputError(f"{label} must be a number.") from None
    if not math.isfinite(number) or number < minimum or number > maximum:
        raise APIInputError(f"{label} must be between {minimum} and {maximum}.")
    return number


def parse_date(value, label):
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError):
        raise APIInputError(f"{label} must use YYYY-MM-DD format.") from None
    return datetime.combine(parsed, datetime.min.time())


def csv_safe(value):
    if isinstance(value, str) and value.lstrip(" \t\r\n").startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def record_audit(action, entity_type, entity_id=None, details=None):
    db.session.add(AuditLog(
        user_id=current_user.id if current_user.is_authenticated else None,
        action=action,
        entity_type=entity_type,
        entity_id=entity_id,
        details=json.dumps(details, ensure_ascii=False) if details is not None else None,
    ))


def commit_or_conflict(message):
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": message}), 409
    return None


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        @login_required
        def wrapper(*a, **kw):
            if current_user.role not in roles:
                abort(403)
            return fn(*a, **kw)
        return wrapper
    return deco


# ---------------------------------------------------------------------------
# Inventory / Product CRUD
# ---------------------------------------------------------------------------

@bp.route("/products")
@roles_required("admin", "instructor", "inventory_clerk", "sales_personnel")
def list_products():
    q = request.args.get("q", "").strip()
    cat = request.args.get("category", "").strip()
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 15, type=int), 1), 100)

    query = Product.query
    if q:
        query = query.filter(
            Product.name.ilike(f"%{q}%") | Product.sku.ilike(f"%{q}%")
        )
    if cat:
        query = query.filter(Product.category == cat)

    query = query.order_by(Product.name)
    pag = query.paginate(page=page, per_page=per_page, error_out=False)

    return jsonify({
        "products": [
            {
                "id": p.id, "sku": p.sku, "name": p.name,
                "category": p.category, "supplier": p.supplier,
                "stock": p.stock, "reorder_level": p.reorder_level,
                "unit_price": float(p.unit_price),
                "status": p.status_label, "status_class": p.status_class,
            }
            for p in pag.items
        ],
        "page": pag.page,
        "pages": pag.pages,
        "total": pag.total,
    })


@bp.route("/products", methods=["POST"])
@roles_required("admin", "inventory_clerk")
def create_product():
    d = json_object()
    sku = required_text(d, "sku", "SKU", 32)
    if Product.query.filter_by(sku=sku).first():
        return jsonify({"error": "SKU already exists"}), 400
    p = Product(
        sku=sku,
        name=required_text(d, "name", "Product name"),
        category=optional_text(d, "category", 64),
        supplier=optional_text(d, "supplier", 120),
        stock=integer_value(d.get("stock", 0), "Stock"),
        reorder_level=integer_value(d.get("reorder_level", 0), "Reorder level"),
        unit_price=money_value(d.get("unit_price", 0), "Unit price"),
    )
    db.session.add(p)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "A product with this SKU already exists."}), 409
    # Log the initial stock
    if p.stock > 0:
        db.session.add(StockLog(
            product_id=p.id, user_id=current_user.id,
            change=p.stock, reason="Initial stock"
        ))
    record_audit("create", "product", p.id, {"sku": p.sku, "name": p.name})
    conflict = commit_or_conflict("A product with this SKU already exists.")
    if conflict:
        return conflict
    return jsonify({"id": p.id, "message": "Product created"}), 201


@bp.route("/products/<int:pid>", methods=["PUT"])
@roles_required("admin", "inventory_clerk")
def update_product(pid):
    p = Product.query.filter_by(id=pid).with_for_update().first_or_404()
    d = json_object()
    old_stock = p.stock

    if "name" in d:
        p.name = required_text(d, "name", "Product name")
    if "category" in d:
        p.category = optional_text(d, "category", 64)
    if "supplier" in d:
        p.supplier = optional_text(d, "supplier", 120)
    if "reorder_level" in d:
        p.reorder_level = integer_value(d["reorder_level"], "Reorder level")
    if "unit_price" in d:
        p.unit_price = money_value(d["unit_price"], "Unit price")

    new_stock = integer_value(d.get("stock", p.stock), "Stock")
    if new_stock != old_stock:
        diff = new_stock - old_stock
        p.stock = new_stock
        db.session.add(StockLog(
            product_id=p.id, user_id=current_user.id,
            change=diff, reason="Manual adjustment"
        ))

    record_audit("update", "product", p.id, {"stock": p.stock})
    conflict = commit_or_conflict("Product update conflicts with an existing record.")
    if conflict:
        return conflict
    return jsonify({"message": "Product updated"})


@bp.route("/products/<int:pid>", methods=["DELETE"])
@roles_required("admin", "inventory_clerk")
def delete_product(pid):
    p = Product.query.filter_by(id=pid).with_for_update().first_or_404()
    if (
        SaleItem.query.filter_by(product_id=pid).first()
        or StockLog.query.filter_by(product_id=pid).first()
        or PurchaseOrderItem.query.filter_by(product_id=pid).first()
    ):
        return jsonify({"error": "Cannot delete a product with sales, stock, or purchase history."}), 409
    record_audit("delete", "product", p.id, {"sku": p.sku, "name": p.name})
    db.session.delete(p)
    conflict = commit_or_conflict("Product history prevents deletion.")
    if conflict:
        return conflict
    return jsonify({"message": "Product deleted"})


@bp.route("/products/categories")
@login_required
def product_categories():
    cats = db.session.query(Product.category).distinct().filter(
        Product.category.isnot(None)
    ).order_by(Product.category).all()
    return jsonify([c[0] for c in cats])


# ---------------------------------------------------------------------------
# Stock management
# ---------------------------------------------------------------------------

@bp.route("/products/<int:pid>/restock", methods=["POST"])
@roles_required("admin", "inventory_clerk")
def restock_product(pid):
    p = Product.query.filter_by(id=pid).with_for_update().first_or_404()
    d = json_object()
    qty = integer_value(d.get("qty"), "Quantity", minimum=1)
    if qty > 2147483647 - p.stock:
        raise APIInputError("The resulting stock would exceed the supported limit.")
    p.stock += qty
    reason = optional_text(d, "reason", 120) or "Restock"
    db.session.add(StockLog(
        product_id=p.id, user_id=current_user.id,
        change=qty, reason=reason
    ))
    record_audit("restock", "product", p.id, {"quantity": qty, "reason": reason})
    db.session.commit()
    return jsonify({"message": f"Added {qty} to stock", "new_stock": p.stock})


@bp.route("/inventory/low-stock")
@roles_required("admin", "instructor", "inventory_clerk")
def low_stock_products():
    products = Product.query.filter(
        Product.stock <= Product.reorder_level
    ).order_by(Product.stock, Product.name).all()
    return jsonify({
        "count": len(products),
        "products": [
            {"id": p.id, "sku": p.sku, "name": p.name, "stock": p.stock,
             "reorder_level": p.reorder_level, "supplier": p.supplier}
            for p in products
        ],
    })


@bp.route("/stock-movements")
@roles_required("admin", "instructor", "inventory_clerk")
def list_stock_movements():
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 25, type=int), 1), 100)
    query = StockLog.query.join(Product).order_by(StockLog.created_at.desc(), StockLog.id.desc())
    product_id = request.args.get("product_id", type=int)
    if product_id:
        query = query.filter(StockLog.product_id == product_id)
    pag = query.paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        "movements": [
            {
                "id": log.id, "product_id": log.product_id, "product": log.product.name,
                "sku": log.product.sku, "user": log.user.full_name if log.user else "System",
                "change": log.change, "reason": log.reason,
                "created_at": log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else None,
            }
            for log in pag.items
        ],
        "page": pag.page, "pages": pag.pages, "total": pag.total,
    })


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------

@bp.route("/sales", methods=["POST"])
@roles_required("admin", "sales_personnel")
def create_sale():
    d = json_object()
    items = d.get("items", [])
    if not isinstance(items, list) or not items:
        raise APIInputError("Add at least one item to the sale.")
    if len(items) > 100:
        raise APIInputError("A sale cannot contain more than 100 line items.")

    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise APIInputError("Each sale item must be an object.")
        pid = integer_value(item.get("product_id"), "Product ID", minimum=1)
        qty = integer_value(item.get("qty"), "Quantity", minimum=1)
        quantities[pid] = quantities.get(pid, 0) + qty

    products = Product.query.filter(Product.id.in_(quantities)).order_by(
        Product.id
    ).with_for_update().all()
    product_by_id = {product.id: product for product in products}
    missing = set(quantities) - set(product_by_id)
    if missing:
        raise APIInputError(f"Product {min(missing)} does not exist.")
    subtotal = Decimal("0")
    for pid, qty in quantities.items():
        product = product_by_id[pid]
        if qty > product.stock:
            raise APIInputError(f"Insufficient stock for {product.name} (available: {product.stock}).")
        subtotal += product.unit_price * qty
    if subtotal > Decimal("99999999.99"):
        raise APIInputError("Sale subtotal exceeds the supported amount.")

    discount = money_value(d.get("discount", 0), "Discount")
    if discount > subtotal:
        raise APIInputError("Discount cannot exceed the sale subtotal.")
    payment_method = d.get("payment_method", "Cash")
    if payment_method != "Cash":
        raise APIInputError("Payment method must be Cash.")
    total = subtotal - discount
    amount_tendered = money_value(d.get("amount_tendered", total), "Amount tendered")
    if amount_tendered < total:
        raise APIInputError("Amount tendered is less than the sale total.")

    customer_name = optional_text(d, "customer", 120)
    customer_record = None
    if d.get("customer_id") is not None:
        customer_id = integer_value(d["customer_id"], "Customer ID", minimum=1)
        customer_record = db.get_or_404(Customer, customer_id)
        customer_name = customer_record.name
    elif customer_name:
        customer_record = Customer.query.filter(
            func.lower(Customer.name) == customer_name.lower()
        ).first()
        if not customer_record:
            customer_record = Customer(name=customer_name)
            db.session.add(customer_record)
            db.session.flush()

    sale = Sale(
        cashier_id=current_user.id,
        customer_id=customer_record.id if customer_record else None,
        customer=customer_name,
        payment_method=payment_method,
        amount_tendered=amount_tendered,
        discount=discount,
        total=total,
    )
    db.session.add(sale)
    db.session.flush()
    for pid, qty in quantities.items():
        product = product_by_id[pid]
        si = SaleItem(sale_id=sale.id, product_id=pid, qty=qty, unit_price=product.unit_price)
        db.session.add(si)
        product.stock -= qty
        db.session.add(StockLog(
            product_id=product.id, user_id=current_user.id,
            change=-qty, reason=f"Sale #{sale.id}"
        ))

    record_audit("create", "sale", sale.id, {"total": str(total), "items": quantities})
    db.session.commit()

    return jsonify({
        "message": "Sale completed",
        "sale_id": sale.id,
        "receipt_no": sale.receipt_no,
        "total": float(sale.total),
        "change": float(sale.change_amount),
    }), 201


@bp.route("/sales")
@roles_required("admin", "instructor", "sales_personnel")
def list_sales():
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 15, type=int), 1), 100)
    query = Sale.query
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if date_from:
        query = query.filter(Sale.created_at >= parse_date(date_from, "Start date"))
    if date_to:
        query = query.filter(Sale.created_at < parse_date(date_to, "End date") + timedelta(days=1))
    if date_from and date_to and parse_date(date_from, "Start date") > parse_date(date_to, "End date"):
        raise APIInputError("Start date must be on or before end date.")
    term = request.args.get("q", "").strip()
    if term:
        filters = [Sale.customer.ilike(f"%{term}%")]
        if term.isdigit():
            filters.append(Sale.id == int(term))
        query = query.filter(or_(*filters))
    pag = query.order_by(Sale.created_at.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return jsonify({
        "sales": [
            {
                "id": s.id,
                "receipt_no": s.receipt_no,
                "created_at": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else None,
                "cashier": s.cashier.full_name if s.cashier else "N/A",
                "customer": s.customer or "Walk-in",
                "payment_method": s.payment_method,
                "discount": float(s.discount or 0),
                "total": float(s.total or 0),
                "returned": sum(item.returned_qty for item in s.items),
                "refund_total": float(sum((r.refund_amount for r in s.returns), Decimal("0"))),
                "items": [
                    {
                        "product": si.product.name if si.product else "Unknown",
                        "qty": si.qty,
                        "unit_price": float(si.unit_price),
                        "subtotal": si.subtotal,
                    }
                    for si in s.items
                ],
            }
            for s in pag.items
        ],
        "page": pag.page,
        "pages": pag.pages,
        "total": pag.total,
    })


@bp.route("/sales/<int:sid>")
@roles_required("admin", "instructor", "sales_personnel")
def get_sale(sid):
    s = db.get_or_404(Sale, sid)
    return jsonify({
        "id": s.id,
        "receipt_no": s.receipt_no,
        "created_at": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else None,
        "cashier": s.cashier.full_name if s.cashier else "N/A",
        "customer": s.customer or "Walk-in",
        "payment_method": s.payment_method,
        "amount_tendered": float(s.amount_tendered or 0),
        "discount": float(s.discount or 0),
        "total": float(s.total or 0),
        "change": float(s.change_amount),
        "items": [
            {
                "product": si.product.name if si.product else "Unknown",
                "qty": si.qty,
                "returned_qty": si.returned_qty,
                "unit_price": float(si.unit_price),
                "subtotal": si.subtotal,
                "sale_item_id": si.id,
            }
            for si in s.items
        ],
        "returns": [
            {"id": r.id, "refund_amount": float(r.refund_amount),
             "reason": r.reason,
             "created_at": r.created_at.strftime("%Y-%m-%d %H:%M") if r.created_at else None}
            for r in sorted(s.returns, key=lambda row: row.created_at or datetime.min)
        ],
    })


@bp.route("/sales/<int:sid>/returns", methods=["POST"])
@roles_required("admin", "sales_personnel")
def create_sale_return(sid):
    sale = Sale.query.filter_by(id=sid).with_for_update().first_or_404()
    data = json_object()
    requested_items = data.get("items")
    if not isinstance(requested_items, list) or not requested_items:
        raise APIInputError("Select at least one item to return.")

    return_quantities = {}
    for item in requested_items:
        if not isinstance(item, dict):
            raise APIInputError("Each returned item must be an object.")
        sale_item_id = integer_value(item.get("sale_item_id"), "Sale item ID", minimum=1)
        qty = integer_value(item.get("qty"), "Return quantity", minimum=1)
        return_quantities[sale_item_id] = return_quantities.get(sale_item_id, 0) + qty

    sale_items = SaleItem.query.filter(
        SaleItem.id.in_(return_quantities),
        SaleItem.sale_id == sale.id,
    ).with_for_update().all()
    sale_item_by_id = {item.id: item for item in sale_items}
    if set(return_quantities) != set(sale_item_by_id):
        raise APIInputError("One or more selected items do not belong to this sale.")
    products = Product.query.filter(
        Product.id.in_([
            sale_item_by_id[item_id].product_id for item_id in return_quantities
        ])
    ).order_by(Product.id).with_for_update().all()
    product_by_id = {product.id: product for product in products}
    for sale_item_id, qty in return_quantities.items():
        sale_item = sale_item_by_id[sale_item_id]
        if qty > sale_item.qty - sale_item.returned_qty:
            raise APIInputError(
                f"Return quantity exceeds the remaining quantity for {product_by_id[sale_item.product_id].name}."
            )
        if qty > 2147483647 - product_by_id[sale_item.product_id].stock:
            raise APIInputError("The returned quantity would exceed the supported stock limit.")

    already_refunded = db.session.query(
        func.coalesce(func.sum(SaleReturn.refund_amount), 0)
    ).filter(SaleReturn.sale_id == sale.id).scalar()
    sale_subtotal = sum((item.unit_price * item.qty for item in sale.items), Decimal("0"))
    sale_total = Decimal(str(sale.total or 0))
    discount_ratio = (
        (sale_total / sale_subtotal) if sale_subtotal > 0 else Decimal("0")
    )
    remaining_refundable = max(Decimal("0"), sale_total - Decimal(str(already_refunded)))
    refund_amount = Decimal("0")
    for sale_item_id, qty in return_quantities.items():
        sale_item = sale_item_by_id[sale_item_id]
        refund_amount += (sale_item.unit_price * qty * discount_ratio).quantize(Decimal("0.01"))
    refund_amount = min(refund_amount, remaining_refundable).quantize(Decimal("0.01"))
    sale_return = SaleReturn(
        sale_id=sale.id,
        user_id=current_user.id,
        reason=optional_text(data, "reason", 255),
        refund_amount=refund_amount,
    )
    db.session.add(sale_return)
    db.session.flush()
    for sale_item_id, qty in return_quantities.items():
        sale_item = sale_item_by_id[sale_item_id]
        sale_item.returned_qty += qty
        product_by_id[sale_item.product_id].stock += qty
        db.session.add(SaleReturnItem(
            return_id=sale_return.id, sale_item_id=sale_item.id,
            qty=qty, unit_price=sale_item.unit_price,
        ))
        db.session.add(StockLog(
            product_id=sale_item.product_id, user_id=current_user.id,
            change=qty, reason=f"Return for sale #{sale.id}",
        ))
    record_audit(
        "return", "sale", sale.id,
        {"return_id": sale_return.id, "refund_amount": str(refund_amount),
         "items": return_quantities},
    )
    db.session.commit()
    return jsonify({
        "message": "Return recorded and stock restored.",
        "return_id": sale_return.id,
        "refund_amount": float(refund_amount),
    }), 201


@bp.route("/customers", methods=["GET", "POST"])
@roles_required("admin", "instructor", "sales_personnel")
def customers():
    if request.method == "GET":
        query = Customer.query
        term = request.args.get("q", "").strip()
        if term:
            query = query.filter(or_(
                Customer.name.ilike(f"%{term}%"),
                Customer.email.ilike(f"%{term}%"),
                Customer.phone.ilike(f"%{term}%"),
            ))
        results = query.order_by(Customer.name).limit(100).all()
        customer_ids = [customer.id for customer in results]
        sale_counts = dict(db.session.query(
            Sale.customer_id, func.count(Sale.id)
        ).filter(Sale.customer_id.in_(customer_ids)).group_by(Sale.customer_id).all()) if customer_ids else {}
        return jsonify([
            {"id": customer.id, "name": customer.name, "email": customer.email,
             "phone": customer.phone,
             "sales_count": sale_counts.get(customer.id, 0)}
            for customer in results
        ])

    if current_user.role == "instructor":
        abort(403)
    data = json_object()
    customer_name = required_text(data, "name", "Customer name")
    if Customer.query.filter(func.lower(Customer.name) == customer_name.lower()).first():
        return jsonify({"error": "A customer with this name already exists."}), 409
    customer = Customer(
        name=customer_name,
        email=optional_email(data),
        phone=optional_text(data, "phone", 32),
    )
    db.session.add(customer)
    db.session.flush()
    record_audit("create", "customer", customer.id, {"name": customer.name})
    db.session.commit()
    return jsonify({"id": customer.id, "message": "Customer created"}), 201


@bp.route("/customers/<int:customer_id>", methods=["PUT", "DELETE"])
@roles_required("admin", "sales_personnel")
def customer_detail(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    if request.method == "DELETE":
        if Sale.query.filter_by(customer_id=customer.id).first():
            return jsonify({"error": "Cannot delete a customer linked to sales history."}), 409
        record_audit("delete", "customer", customer.id, {"name": customer.name})
        db.session.delete(customer)
        conflict = commit_or_conflict("Customer sales history prevents deletion.")
        if conflict:
            return conflict
        return jsonify({"message": "Customer deleted"})

    data = json_object()
    if "name" in data:
        new_name = required_text(data, "name", "Customer name")
        duplicate = Customer.query.filter(
            func.lower(Customer.name) == new_name.lower(),
            Customer.id != customer.id,
        ).first()
        if duplicate:
            return jsonify({"error": "A customer with this name already exists."}), 409
        Sale.query.filter_by(customer_id=customer.id).update(
            {"customer": new_name}, synchronize_session=False
        )
        customer.name = new_name
    if "email" in data:
        customer.email = optional_email(data)
    if "phone" in data:
        customer.phone = optional_text(data, "phone", 32)
    record_audit("update", "customer", customer.id, {"name": customer.name})
    db.session.commit()
    return jsonify({"message": "Customer updated"})


@bp.route("/customers/<int:customer_id>/sales")
@roles_required("admin", "instructor", "sales_personnel")
def customer_sales(customer_id):
    customer = db.get_or_404(Customer, customer_id)
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 20, type=int), 1), 100)
    pag = Sale.query.filter_by(customer_id=customer.id).order_by(
        Sale.created_at.desc()
    ).paginate(page=page, per_page=per_page, error_out=False)
    return jsonify({
        "customer": customer.name,
        "sales": [
            {"id": sale.id, "receipt_no": sale.receipt_no,
             "created_at": sale.created_at.strftime("%Y-%m-%d %H:%M") if sale.created_at else None,
             "payment_method": sale.payment_method, "total": float(sale.total or 0),
             "refunds": float(sum((row.refund_amount for row in sale.returns), Decimal("0")))}
            for sale in pag.items
        ],
        "page": pag.page, "pages": pag.pages, "total": pag.total,
    })


# ---------------------------------------------------------------------------
# Dashboard stats
# ---------------------------------------------------------------------------

@bp.route("/dashboard/stats")
@roles_required("admin", "instructor")
def dashboard_stats():
    today = datetime.utcnow().date()
    today_start = datetime.combine(today, datetime.min.time())

    # Sales today
    sales_today = db.session.query(func.coalesce(func.sum(Sale.total), 0)).filter(
        Sale.created_at >= today_start
    ).scalar()

    # Low stock items
    low_stock = Product.query.filter(Product.stock <= Product.reorder_level).count()

    # Total products
    total_products = Product.query.count()

    # Total users
    total_users = User.query.count()

    # Total sales count
    total_sales = Sale.query.count()

    # Best selling product (by qty sold)
    best_seller = db.session.query(
        Product.name, func.sum(SaleItem.qty - SaleItem.returned_qty).label("total_qty")
    ).join(SaleItem, SaleItem.product_id == Product.id
    ).filter(SaleItem.qty > SaleItem.returned_qty
    ).group_by(Product.id, Product.name
    ).order_by(desc("total_qty")
    ).first()

    # Recent 5 transactions
    recent_sales = Sale.query.order_by(Sale.created_at.desc()).limit(5).all()

    # Monthly sales data for chart (last 12 months)
    monthly_sales = []
    for offset in range(11, -1, -1):
        month_index = today.year * 12 + today.month - 1 - offset
        year, zero_based_month = divmod(month_index, 12)
        month = zero_based_month + 1
        month_start = datetime(year, month, 1)
        if month == 12:
            month_end = datetime(year + 1, 1, 1)
        else:
            month_end = datetime(year, month + 1, 1)
        total = db.session.query(func.coalesce(func.sum(Sale.total), 0)).filter(
            Sale.created_at >= month_start, Sale.created_at < month_end
        ).scalar()
        monthly_sales.append({
            "month": month_start.strftime("%b %Y"),
            "total": float(total),
        })

    # Top 5 products by quantity sold
    top_products = db.session.query(
        Product.name, func.sum(SaleItem.qty - SaleItem.returned_qty).label("total_qty")
    ).join(SaleItem, SaleItem.product_id == Product.id
    ).filter(SaleItem.qty > SaleItem.returned_qty
    ).group_by(Product.id, Product.name
    ).order_by(desc("total_qty")
    ).limit(5).all()

    # Slow moving products (no sale in 30+ days)
    thirty_days_ago = today_start - timedelta(days=30)
    subq = db.session.query(SaleItem.product_id).join(Sale).filter(
        Sale.created_at >= thirty_days_ago
    ).distinct().subquery()
    slow_products = Product.query.filter(
        Product.stock > 0,
        ~Product.id.in_(db.session.query(subq))
    ).order_by(Product.stock.desc()).limit(5).all()

    return jsonify({
        "sales_today": float(sales_today),
        "low_stock": low_stock,
        "total_products": total_products,
        "total_users": total_users,
        "total_sales": total_sales,
        "best_seller": best_seller[0] if best_seller else "N/A",
        "best_seller_qty": best_seller[1] if best_seller else 0,
        "monthly_sales": monthly_sales,
        "top_products": [
            {"name": p[0], "qty": int(p[1])} for p in top_products
        ],
        "slow_products": [
            {"name": p.name, "stock": p.stock, "category": p.category}
            for p in slow_products
        ],
        "recent_sales": [
            {
                "receipt_no": s.receipt_no,
                "cashier": s.cashier.full_name if s.cashier else "N/A",
                "total": float(s.total or 0),
                "date": s.created_at.strftime("%Y-%m-%d %H:%M") if s.created_at else None,
            }
            for s in recent_sales
        ],
    })


def sales_report_query():
    query = Sale.query
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    if date_from:
        query = query.filter(Sale.created_at >= parse_date(date_from, "Start date"))
    if date_to:
        query = query.filter(Sale.created_at < parse_date(date_to, "End date") + timedelta(days=1))
    if date_from and date_to and parse_date(date_from, "Start date") > parse_date(date_to, "End date"):
        raise APIInputError("Start date must be on or before end date.")
    return query


@bp.route("/reports/sales")
@roles_required("admin", "instructor")
def sales_report():
    query = sales_report_query()
    aggregates = query.with_entities(
        func.count(Sale.id),
        func.coalesce(func.sum(Sale.total), 0),
        func.coalesce(func.sum(Sale.discount), 0),
    ).first()
    sale_ids = query.with_entities(Sale.id).statement
    refunds = db.session.query(
        func.coalesce(func.sum(SaleReturn.refund_amount), 0)
    ).filter(SaleReturn.sale_id.in_(sale_ids)).scalar()
    sale_totals = Decimal(str(aggregates[1]))
    discounts = Decimal(str(aggregates[2]))
    refund_total = Decimal(str(refunds))
    return jsonify({
        "transaction_count": int(aggregates[0]),
        "gross_sales": float(sale_totals + discounts),
        "discounts": float(discounts),
        "refunds": float(refund_total),
        "net_sales": float(sale_totals - refund_total),
        "date_from": request.args.get("date_from"),
        "date_to": request.args.get("date_to"),
    })


@bp.route("/reports/sales.csv")
@roles_required("admin", "instructor")
def export_sales_report():
    rows = sales_report_query().order_by(Sale.created_at.desc()).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Receipt", "Date", "Cashier", "Customer", "Payment", "Discount", "Total", "Refunds", "Net"])
    for sale in rows:
        refunds = sum((item.refund_amount for item in sale.returns), Decimal("0"))
        writer.writerow([
            csv_safe(sale.receipt_no),
            sale.created_at.strftime("%Y-%m-%d %H:%M") if sale.created_at else "",
            csv_safe(sale.cashier.full_name if sale.cashier else ""),
            csv_safe(sale.customer or "Walk-in"),
            csv_safe(sale.payment_method),
            sale.discount or 0,
            sale.total or 0,
            refunds,
            Decimal(str(sale.total or 0)) - refunds,
        ])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename="sales-report.csv"'
    return response


@bp.route("/reports/inventory")
@roles_required("admin", "instructor")
def inventory_report():
    products = Product.query.order_by(Product.name).all()
    return jsonify({
        "product_count": len(products),
        "low_stock_count": sum(p.stock <= p.reorder_level for p in products),
        "inventory_value": float(sum(
            (p.unit_price * p.stock for p in products), Decimal("0")
        )),
        "products": [
            {"id": p.id, "sku": p.sku, "name": p.name, "category": p.category,
             "stock": p.stock, "reorder_level": p.reorder_level,
             "unit_price": float(p.unit_price),
             "inventory_value": float(p.unit_price * p.stock),
             "status": p.status_label}
            for p in products
        ],
    })


@bp.route("/reports/inventory.csv")
@roles_required("admin", "instructor")
def export_inventory_report():
    products = Product.query.order_by(Product.name).all()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["SKU", "Product", "Category", "Stock", "Reorder Level", "Unit Price", "Inventory Value", "Status"])
    for product in products:
        writer.writerow([
            csv_safe(product.sku), csv_safe(product.name), csv_safe(product.category or ""), product.stock,
            product.reorder_level, product.unit_price,
            product.unit_price * product.stock, product.status_label,
        ])
    response = make_response("\ufeff" + output.getvalue())
    response.headers["Content-Type"] = "text/csv; charset=utf-8"
    response.headers["Content-Disposition"] = 'attachment; filename="inventory-report.csv"'
    return response


@bp.route("/audit-logs")
@roles_required("admin")
def list_audit_logs():
    page = max(request.args.get("page", 1, type=int), 1)
    per_page = min(max(request.args.get("per_page", 30, type=int), 1), 100)
    pag = AuditLog.query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).paginate(
        page=page, per_page=per_page, error_out=False
    )
    return jsonify({
        "logs": [
            {
                "id": log.id, "user": log.user.username if log.user else "System",
                "action": log.action, "entity_type": log.entity_type,
                "entity_id": log.entity_id,
                "details": json.loads(log.details) if log.details else None,
                "created_at": log.created_at.strftime("%Y-%m-%d %H:%M") if log.created_at else None,
            }
            for log in pag.items
        ],
        "page": pag.page, "pages": pag.pages, "total": pag.total,
    })


# ---------------------------------------------------------------------------
# Knowledge Analysis & Prediction
# ---------------------------------------------------------------------------

@bp.route("/knowledge/train", methods=["POST"])
@roles_required("admin", "instructor")
def knowledge_train():
    from . import knowledge as ka
    d = json_object()
    pred_type = d.get("prediction_type")
    model_name = d.get("model", "slr")
    if not isinstance(model_name, str) or model_name not in {"slr", "random_forest"}:
        raise APIInputError("Model must be 'slr' or 'random_forest'.")

    trainers = {
        "demand": ka.train_demand,
        "sales": ka.train_sales,
        "slow_moving": ka.train_slow_moving,
        "best_selling": ka.train_best_selling,
    }

    if not isinstance(pred_type, str):
        raise APIInputError("Choose a prediction type.")
    trainer = trainers.get(pred_type)
    if not trainer:
        return jsonify({"error": f"Unknown prediction type: {pred_type}"}), 400

    try:
        metrics = trainer(model_name)
    except FileNotFoundError:
        return jsonify({"error": "The training dataset is unavailable."}), 404
    except ValueError as error:
        return jsonify({"error": str(error)}), 422

    # Add descriptions
    described_metrics = {}
    for key, value in metrics.items():
        described_metrics[key] = {
            "value": value,
            "description": ka.METRIC_DESCRIPTIONS.get(key, ""),
        }

    # Save to prediction log
    log = PredictionLog(
        user_id=current_user.id,
        prediction_type=pred_type,
        model_used=model_name,
        input_params=json.dumps({"action": "train"}),
        result=json.dumps({"status": "trained"}),
        metrics=json.dumps(metrics),
    )
    db.session.add(log)
    record_audit("train_model", "prediction_model", None, {
        "prediction_type": pred_type, "model": model_name,
    })
    db.session.commit()

    return jsonify({
        "message": f"Model trained and saved successfully",
        "model": model_name,
        "prediction_type": pred_type,
        "metrics": described_metrics,
    })


@bp.route("/knowledge/predict", methods=["POST"])
@roles_required("admin", "instructor")
def knowledge_predict():
    from . import knowledge as ka
    d = json_object()
    pred_type = d.get("prediction_type")
    model_name = d.get("model", "slr")
    params = d.get("params", {})
    if not isinstance(pred_type, str) or pred_type not in {
        "demand", "sales", "slow_moving", "best_selling"
    }:
        raise APIInputError(f"Unknown prediction type: {pred_type}")
    if not isinstance(model_name, str) or model_name not in {"slr", "random_forest"}:
        raise APIInputError("Model must be 'slr' or 'random_forest'.")
    if not isinstance(params, dict):
        raise APIInputError("Prediction parameters must be a JSON object.")

    try:
        if pred_type == "demand":
            month = integer_value(params.get("month", 1), "Month", 1)
            day_of_week = integer_value(params.get("day_of_week", 0), "Day of week", 0)
            school_season = integer_value(params.get("is_school_season", 0), "School season", 0)
            if month > 12 or day_of_week > 6 or school_season > 1:
                raise APIInputError("Month must be 1-12, day of week 0-6, and school season 0 or 1.")
            result = ka.predict_demand(
                model_name,
                month=month,
                day_of_week=day_of_week,
                is_school_season=school_season,
                unit_price=finite_number(params.get("unit_price", 0), "Unit price"),
                stock_level=integer_value(params.get("stock_level", 0), "Stock level"),
            )
            result_data = {"predicted_qty": result}
        elif pred_type == "sales":
            month = integer_value(params.get("month", 1), "Month", 1)
            if month > 12:
                raise APIInputError("Month must be between 1 and 12.")
            result = ka.predict_sales(
                model_name,
                month=month,
                year=integer_value(params.get("year", 2026), "Year", 2000),
                qty_sold=integer_value(params.get("qty_sold", 0), "Quantity sold"),
                unit_price=finite_number(params.get("unit_price", 0), "Unit price"),
                discount_pct=finite_number(params.get("discount_pct", 0), "Discount percentage", 0, 100),
            )
            result_data = {"predicted_amount": result}
        elif pred_type == "slow_moving":
            result = ka.predict_slow_moving(
                model_name,
                unit_price=finite_number(params.get("unit_price", 0), "Unit price"),
                avg_monthly_sales=finite_number(params.get("avg_monthly_sales", 0), "Average monthly sales"),
                stock_level=integer_value(params.get("stock_level", 0), "Stock level"),
                days_since_last_sale=integer_value(params.get("days_since_last_sale", 0), "Days since last sale"),
                reorder_level=integer_value(params.get("reorder_level", 0), "Reorder level"),
                turnover_rate=finite_number(params.get("turnover_rate", 0), "Turnover rate"),
            )
            result_data = {
                "classification": "Slow-Moving" if result == 1 else "Fast-Moving",
                "is_slow_moving": result,
            }
        elif pred_type == "best_selling":
            result = ka.predict_best_selling(
                model_name,
                unit_price=finite_number(params.get("unit_price", 0), "Unit price"),
                total_qty_sold=integer_value(params.get("total_qty_sold", 0), "Total quantity sold"),
                total_revenue=finite_number(params.get("total_revenue", 0), "Total revenue"),
                avg_monthly_sales=finite_number(params.get("avg_monthly_sales", 0), "Average monthly sales"),
                months_available=integer_value(params.get("months_available", 0), "Months available"),
                popularity_score=finite_number(params.get("popularity_score", 0), "Popularity score"),
            )
            result_data = {
                "classification": "Best-Seller" if result == 1 else "Regular Product",
                "is_best_seller": result,
            }
        else:
            return jsonify({"error": f"Unknown prediction type: {pred_type}"}), 400
    except FileNotFoundError:
        return jsonify({"error": "The prediction model is unavailable."}), 404
    except ValueError as error:
        return jsonify({"error": str(error)}), 400

    # Save to prediction log
    log = PredictionLog(
        user_id=current_user.id,
        prediction_type=pred_type,
        model_used=model_name,
        input_params=json.dumps(params),
        result=json.dumps(result_data),
        metrics=None,
    )
    db.session.add(log)
    record_audit("predict", "prediction_model", None, {
        "prediction_type": pred_type, "model": model_name,
    })
    db.session.commit()

    return jsonify({
        "prediction_type": pred_type,
        "model": model_name,
        "result": result_data,
    })


@bp.route("/knowledge/datasets")
@roles_required("admin", "instructor")
def list_datasets():
    from . import knowledge as ka
    return jsonify(ka.get_dataset_info())


@bp.route("/knowledge/datasets/<name>")
@roles_required("admin", "instructor")
def preview_dataset(name):
    from . import knowledge as ka
    known_datasets = {item["name"] for item in ka.get_dataset_info()}
    if name not in known_datasets:
        return jsonify({"error": "Dataset not found."}), 404
    rows = min(max(request.args.get("rows", 20, type=int), 1), 100)
    return jsonify(ka.get_dataset_preview(name, rows=rows))


# ---------------------------------------------------------------------------
# User management (admin)
# ---------------------------------------------------------------------------

@bp.route("/users")
@roles_required("admin")
def list_users():
    users = User.query.order_by(User.id).all()
    return jsonify([
        {
            "id": u.id, "full_name": u.full_name, "username": u.username,
            "email": u.email, "role": u.role, "role_label": u.role_label,
            "status": u.status,
            "created_at": u.created_at.strftime("%Y-%m-%d") if u.created_at else None,
        }
        for u in users
    ])


@bp.route("/users/<int:uid>/role", methods=["PUT"])
@roles_required("admin")
def change_role(uid):
    user = db.get_or_404(User, uid)
    if user.id == current_user.id:
        return jsonify({"error": "Cannot change your own role"}), 400
    d = json_object()
    role = d.get("role")
    valid_roles = {"admin", "instructor", "inventory_clerk", "sales_personnel"}
    if role not in valid_roles:
        return jsonify({"error": f"Invalid role: {role}"}), 400
    old_role = user.role
    if old_role == "admin" and role != "admin" and User.query.filter_by(
        role="admin", status="active"
    ).count() <= 1:
        return jsonify({"error": "At least one active admin account must remain."}), 400
    user.role = role
    record_audit("change_role", "user", user.id, {"from": old_role, "to": role})
    db.session.commit()
    return jsonify({"message": f"Role changed to {role}"})


# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

@bp.route("/suppliers")
@roles_required("admin", "inventory_clerk")
def list_suppliers():
    suppliers = Supplier.query.order_by(Supplier.name).all()
    return jsonify([
        {
            "id": s.id, "name": s.name, "contact_person": s.contact_person,
            "phone": s.phone, "email": s.email, "address": s.address,
        }
        for s in suppliers
    ])


@bp.route("/suppliers", methods=["POST"])
@roles_required("admin", "inventory_clerk")
def create_supplier():
    d = json_object()
    supplier_name = required_text(d, "name", "Supplier name")
    if Supplier.query.filter(func.lower(Supplier.name) == supplier_name.lower()).first():
        return jsonify({"error": "A supplier with this name already exists."}), 409
    s = Supplier(
        name=supplier_name,
        contact_person=optional_text(d, "contact_person", 120),
        phone=optional_text(d, "phone", 32),
        email=optional_email(d),
        address=optional_text(d, "address", 2000),
    )
    db.session.add(s)
    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "A supplier with this name already exists."}), 409
    record_audit("create", "supplier", s.id, {"name": s.name})
    conflict = commit_or_conflict("A supplier with this name already exists.")
    if conflict:
        return conflict
    return jsonify({"id": s.id, "message": "Supplier created"}), 201


@bp.route("/suppliers/<int:supplier_id>", methods=["PUT"])
@roles_required("admin", "inventory_clerk")
def update_supplier(supplier_id):
    supplier = db.get_or_404(Supplier, supplier_id)
    data = json_object()
    old_name = supplier.name
    if "name" in data:
        supplier.name = required_text(data, "name", "Supplier name")
        if supplier.name != old_name:
            products = Product.query.filter_by(supplier=old_name).order_by(
                Product.id
            ).with_for_update().all()
            for product in products:
                product.supplier = supplier.name
    for field, max_length in (("contact_person", 120), ("phone", 32),
                              ("email", 120), ("address", 2000)):
        if field in data:
            setattr(
                supplier, field,
                optional_email(data, field) if field == "email"
                else optional_text(data, field, max_length),
            )
    record_audit("update", "supplier", supplier.id, {"name": supplier.name})
    conflict = commit_or_conflict("A supplier with this name already exists.")
    if conflict:
        return conflict
    return jsonify({"message": "Supplier updated"})


@bp.route("/suppliers/<int:supplier_id>", methods=["DELETE"])
@roles_required("admin", "inventory_clerk")
def delete_supplier(supplier_id):
    supplier = db.get_or_404(Supplier, supplier_id)
    if Product.query.filter_by(supplier=supplier.name).first():
        return jsonify({"error": "Update or remove this supplier from its products before deleting it."}), 409
    if PurchaseOrder.query.filter_by(supplier_id=supplier.id).first():
        return jsonify({"error": "Cannot delete a supplier with purchase order history."}), 409
    record_audit("delete", "supplier", supplier.id, {"name": supplier.name})
    db.session.delete(supplier)
    conflict = commit_or_conflict("Supplier history prevents deletion.")
    if conflict:
        return conflict
    return jsonify({"message": "Supplier deleted"})


@bp.route("/purchase-orders", methods=["GET", "POST"])
@roles_required("admin", "inventory_clerk")
def purchase_orders():
    if request.method == "GET":
        page = max(request.args.get("page", 1, type=int), 1)
        per_page = min(max(request.args.get("per_page", 20, type=int), 1), 100)
        pag = PurchaseOrder.query.order_by(PurchaseOrder.created_at.desc()).paginate(
            page=page, per_page=per_page, error_out=False
        )
        return jsonify({
            "orders": [
                {
                    "id": order.id, "supplier": order.supplier.name,
                    "status": order.status, "note": order.note,
                    "created_at": order.created_at.strftime("%Y-%m-%d %H:%M"),
                    "received_at": order.received_at.strftime("%Y-%m-%d %H:%M") if order.received_at else None,
                    "items": [
                        {"product_id": item.product_id, "product": item.product.name,
                         "sku": item.product.sku, "quantity": item.quantity_ordered,
                         "unit_cost": float(item.unit_cost)}
                        for item in order.items
                    ],
                }
                for order in pag.items
            ],
            "page": pag.page, "pages": pag.pages, "total": pag.total,
        })

    data = json_object()
    supplier_id = integer_value(data.get("supplier_id"), "Supplier ID", minimum=1)
    supplier = db.get_or_404(Supplier, supplier_id)
    items = data.get("items")
    if not isinstance(items, list) or not items or len(items) > 100:
        raise APIInputError("A purchase order must contain between 1 and 100 items.")
    quantities = {}
    for item in items:
        if not isinstance(item, dict):
            raise APIInputError("Each purchase order item must be an object.")
        pid = integer_value(item.get("product_id"), "Product ID", minimum=1)
        qty = integer_value(item.get("quantity"), "Quantity", minimum=1)
        unit_cost = money_value(item.get("unit_cost", 0), "Unit cost")
        if pid in quantities:
            raise APIInputError("A product can only appear once per purchase order.")
        quantities[pid] = (qty, unit_cost)
    products = Product.query.filter(Product.id.in_(quantities)).all()
    if len(products) != len(quantities):
        raise APIInputError("One or more selected products do not exist.")
    order = PurchaseOrder(
        supplier_id=supplier.id,
        user_id=current_user.id,
        note=optional_text(data, "note", 255),
    )
    db.session.add(order)
    db.session.flush()
    for product in products:
        qty, unit_cost = quantities[product.id]
        db.session.add(PurchaseOrderItem(
            order_id=order.id, product_id=product.id,
            quantity_ordered=qty, unit_cost=unit_cost,
        ))
    record_audit("create", "purchase_order", order.id, {
        "supplier": supplier.name, "items": {
            pid: qty for pid, (qty, _cost) in quantities.items()
        },
    })
    db.session.commit()
    return jsonify({"id": order.id, "message": "Purchase order created"}), 201


@bp.route("/purchase-orders/<int:order_id>/receive", methods=["POST"])
@roles_required("admin", "inventory_clerk")
def receive_purchase_order(order_id):
    order = PurchaseOrder.query.filter_by(id=order_id).with_for_update().first_or_404()
    if order.status == "received":
        return jsonify({"error": "This purchase order has already been received."}), 409
    if not order.items:
        return jsonify({"error": "This purchase order has no items."}), 409
    products = Product.query.filter(
        Product.id.in_([item.product_id for item in order.items])
    ).order_by(Product.id).with_for_update().all()
    product_by_id = {product.id: product for product in products}
    for item in order.items:
        product = product_by_id[item.product_id]
        if item.quantity_ordered > 2147483647 - product.stock:
            raise APIInputError(f"Receiving this order would exceed stock capacity for {product.name}.")
    for item in order.items:
        product = product_by_id[item.product_id]
        product.stock += item.quantity_ordered
        db.session.add(StockLog(
            product_id=item.product_id, user_id=current_user.id,
            change=item.quantity_ordered, reason=f"Purchase order #{order.id}",
        ))
    order.status = "received"
    order.received_at = datetime.utcnow()
    record_audit("receive", "purchase_order", order.id, {"supplier": order.supplier.name})
    db.session.commit()
    return jsonify({"message": "Purchase order received and stock updated."})
