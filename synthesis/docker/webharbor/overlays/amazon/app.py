#!/usr/bin/env python3
"""Amazon.com mirror - Flask application with full CRUD."""
import os
import json
import random
import re
from datetime import datetime, timedelta

from flask import (Flask, render_template, request, redirect, url_for,
                   flash, jsonify, session, make_response)
from flask_sqlalchemy import SQLAlchemy
from flask_login import (LoginManager, UserMixin, login_user, logout_user,
                         login_required, current_user)
from flask_wtf import FlaskForm
from flask_wtf.csrf import CSRFProtect, generate_csrf
from flask_bcrypt import Bcrypt
from wtforms import StringField, PasswordField, TextAreaField, IntegerField, SelectField
from wtforms.validators import DataRequired, Email, Length, EqualTo, Optional, NumberRange
from sqlalchemy import func

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__)
app.config['SECRET_KEY'] = 'amazon-mirror-secret-key-change-in-prod'
app.config['SQLALCHEMY_DATABASE_URI'] = f"sqlite:///{os.path.join(BASE_DIR, 'instance', 'amazon_store.db')}"
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.config['WTF_CSRF_TIME_LIMIT'] = None

os.makedirs(os.path.join(BASE_DIR, 'instance'), exist_ok=True)

db = SQLAlchemy(app)
bcrypt = Bcrypt(app)
login_manager = LoginManager(app)
login_manager.login_view = 'login'
login_manager.login_message = 'Please sign in to access your account.'
login_manager.login_message_category = 'info'
csrf = CSRFProtect(app)


# ----- Models -----

class User(db.Model, UserMixin):
    __tablename__ = 'users'
    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(120), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(120), nullable=False)
    phone = db.Column(db.String(30), default='')
    address_line1 = db.Column(db.String(200), default='')
    address_line2 = db.Column(db.String(200), default='')
    city = db.Column(db.String(100), default='')
    state = db.Column(db.String(50), default='')
    zip_code = db.Column(db.String(20), default='')
    country = db.Column(db.String(50), default='United States')
    is_prime = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    cart_items = db.relationship('CartItem', backref='user', lazy=True, cascade='all, delete-orphan')
    orders = db.relationship('Order', backref='user', lazy=True, cascade='all, delete-orphan')
    wishlist_items = db.relationship('WishlistItem', backref='user', lazy=True, cascade='all, delete-orphan')
    reviews = db.relationship('Review', backref='user', lazy=True, cascade='all, delete-orphan')
    payment_methods = db.relationship('PaymentMethod', backref='user', lazy=True, cascade='all, delete-orphan')
    saved_addresses = db.relationship('SavedAddress', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, pw):
        self.password_hash = bcrypt.generate_password_hash(pw).decode('utf-8')

    def check_password(self, pw):
        return bcrypt.check_password_hash(self.password_hash, pw)


class Category(db.Model):
    __tablename__ = 'categories'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    slug = db.Column(db.String(80), unique=True, nullable=False, index=True)
    icon = db.Column(db.String(20), default='')
    description = db.Column(db.Text, default='')


class Product(db.Model):
    __tablename__ = 'products'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    slug = db.Column(db.String(255), unique=True, nullable=False, index=True)
    brand = db.Column(db.String(100), default='')
    category_slug = db.Column(db.String(80), index=True)
    subcategory = db.Column(db.String(80), default='')
    description = db.Column(db.Text, default='')
    features = db.Column(db.Text, default='[]')  # JSON list
    specs = db.Column(db.Text, default='{}')  # JSON dict
    price = db.Column(db.Float, nullable=False)
    list_price = db.Column(db.Float, default=0.0)  # original price (strike-through)
    image = db.Column(db.String(500), default='')  # main thumbnail
    gallery_images = db.Column(db.Text, default='[]')  # JSON list of paths
    variant_options = db.Column(db.Text, default='{}')  # JSON dict: colors, sizes
    stock = db.Column(db.Integer, default=100)
    rating = db.Column(db.Float, default=4.5)
    review_count = db.Column(db.Integer, default=0)
    is_prime = db.Column(db.Boolean, default=True)
    is_featured = db.Column(db.Boolean, default=False)
    is_deal = db.Column(db.Boolean, default=False)
    is_bestseller = db.Column(db.Boolean, default=False)
    deal_discount = db.Column(db.Integer, default=0)  # percent off
    free_shipping = db.Column(db.Boolean, default=True)
    free_returns = db.Column(db.Boolean, default=True)
    condition = db.Column(db.String(30), default='New')  # New, Used - Like New, Used - Good, Used - Acceptable, Refurbished
    feature_tags = db.Column(db.Text, default='[]')  # JSON list — searchable feature keywords for filtering
    release_date = db.Column(db.String(20), default='')  # YYYY-MM-DD or "Pre-order: 2024-12-01"
    return_policy = db.Column(db.Text, default='30-day return policy. Eligible for free returns.')
    delivery_estimate = db.Column(db.String(100), default='FREE delivery in 2 days')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    cart_items = db.relationship('CartItem', backref='product', lazy=True, cascade='all, delete-orphan')
    order_items = db.relationship('OrderItem', backref='product', lazy=True)
    wishlist_items = db.relationship('WishlistItem', backref='product', lazy=True, cascade='all, delete-orphan')
    reviews = db.relationship('Review', backref='product', lazy=True, cascade='all, delete-orphan')

    def get_features(self):
        try:
            return json.loads(self.features or '[]')
        except Exception:
            return []

    def get_specs(self):
        try:
            return json.loads(self.specs or '{}')
        except Exception:
            return {}

    def get_gallery(self):
        try:
            return json.loads(self.gallery_images or '[]')
        except Exception:
            return []

    def get_variants(self):
        try:
            v = json.loads(self.variant_options or '{}')
            # Keys starting with "_" are rendering metadata (e.g. per-color images), not options.
            return {k: o for k, o in v.items() if not k.startswith('_')}
        except Exception:
            return {}

    def get_variant_images(self):
        """Optional {color_label: image_path} map from variant_options['_color_images'] for swatch image swaps."""
        try:
            return json.loads(self.variant_options or '{}').get('_color_images', {}) or {}
        except Exception:
            return {}

    def discount_percent(self):
        if self.list_price and self.list_price > self.price:
            return int(round((self.list_price - self.price) / self.list_price * 100))
        return 0

    def get_feature_tags(self):
        try:
            return json.loads(self.feature_tags or '[]')
        except Exception:
            return []

    def get_colors(self):
        return self.get_variants().get('color', []) or []

    def get_sizes(self):
        return self.get_variants().get('size', []) or []


class CartItem(db.Model):
    __tablename__ = 'cart_items'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    quantity = db.Column(db.Integer, default=1)
    variant = db.Column(db.String(100), default='')
    added_at = db.Column(db.DateTime, default=datetime.utcnow)


class Order(db.Model):
    __tablename__ = 'orders'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    order_number = db.Column(db.String(40), unique=True, nullable=False)
    status = db.Column(db.String(30), default='processing')  # processing, shipped, delivered, cancelled
    subtotal = db.Column(db.Float, default=0)
    shipping = db.Column(db.Float, default=0)
    tax = db.Column(db.Float, default=0)
    total = db.Column(db.Float, default=0)
    ship_name = db.Column(db.String(120), default='')
    ship_address = db.Column(db.String(200), default='')
    ship_city = db.Column(db.String(100), default='')
    ship_state = db.Column(db.String(50), default='')
    ship_zip = db.Column(db.String(20), default='')
    payment_method = db.Column(db.String(30), default='Credit Card')
    payment_last4 = db.Column(db.String(10), default='1234')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    delivery_estimate = db.Column(db.String(50), default='')

    items = db.relationship('OrderItem', backref='order', lazy=True, cascade='all, delete-orphan')

    def item_count(self):
        return sum(i.quantity for i in self.items)


class OrderItem(db.Model):
    __tablename__ = 'order_items'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    product_name = db.Column(db.String(255), default='')
    product_image = db.Column(db.String(500), default='')
    variant = db.Column(db.String(100), default='')
    quantity = db.Column(db.Integer, default=1)
    price = db.Column(db.Float, default=0)


class WishlistItem(db.Model):
    __tablename__ = 'wishlist_items'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)


class Review(db.Model):
    __tablename__ = 'reviews'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    product_id = db.Column(db.Integer, db.ForeignKey('products.id'), nullable=False)
    rating = db.Column(db.Integer, default=5)
    title = db.Column(db.String(200), default='')
    body = db.Column(db.Text, default='')
    verified = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PaymentMethod(db.Model):
    __tablename__ = 'payment_methods'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    card_type = db.Column(db.String(20), nullable=False)  # Visa, Mastercard, Amex, Discover
    last4 = db.Column(db.String(4), nullable=False)
    exp_month = db.Column(db.Integer, nullable=False)
    exp_year = db.Column(db.Integer, nullable=False)
    cardholder_name = db.Column(db.String(120), default='')
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SavedAddress(db.Model):
    __tablename__ = 'saved_addresses'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    label = db.Column(db.String(30), default='Home')  # Home, Work, Other
    full_name = db.Column(db.String(120), default='')
    phone = db.Column(db.String(30), default='')
    address_line1 = db.Column(db.String(200), nullable=False)
    address_line2 = db.Column(db.String(200), default='')
    city = db.Column(db.String(100), nullable=False)
    state = db.Column(db.String(50), nullable=False)
    zip_code = db.Column(db.String(20), nullable=False)
    country = db.Column(db.String(50), default='United States')
    is_default = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Return(db.Model):
    __tablename__ = 'returns'
    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey('orders.id'), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id'), nullable=False)
    status = db.Column(db.String(30), default='requested')  # requested, approved, completed
    refund_method = db.Column(db.String(30), default='original_payment')  # original_payment, gift_card
    refund_amount = db.Column(db.Float, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    order = db.relationship('Order', backref='returns')
    items = db.relationship('ReturnItem', backref='return_request', lazy=True, cascade='all, delete-orphan')


class ReturnItem(db.Model):
    __tablename__ = 'return_items'
    id = db.Column(db.Integer, primary_key=True)
    return_id = db.Column(db.Integer, db.ForeignKey('returns.id'), nullable=False)
    order_item_id = db.Column(db.Integer, db.ForeignKey('order_items.id'), nullable=True)
    product_name = db.Column(db.String(255), default='')
    quantity = db.Column(db.Integer, default=1)
    reason = db.Column(db.String(60), default='')  # doesnt_fit, defective, changed_mind, wrong_item


# ----- Login -----

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))


# ----- Persona auto-login -----
# Requests act as the served DB's persona account (found by its fixed seeded password hash) unless WEBHARBOR_NO_AUTOLOGIN is set.
_PERSONA_STD_HASH = "$2b$12$jrXZNk07IJem7HFWOqRXmuoBjGy1dOxwtO/XdxU81zuLSmV4rJ4.y"
_PERSONA_AUTOLOGIN = os.environ.get("WEBHARBOR_NO_AUTOLOGIN", "").strip() == ""


@app.before_request
def _auto_login_persona():
    if not _PERSONA_AUTOLOGIN or request.endpoint == 'static':
        return
    if current_user.is_authenticated:
        return
    try:
        persona = User.query.filter_by(password_hash=_PERSONA_STD_HASH).first()
    except Exception:  # noqa: BLE001 — DB not ready / mid-swap; fall back to anon
        persona = None
    if persona is not None:
        login_user(persona)


# ----- Context processors -----

@app.context_processor
def inject_globals():
    cart_count = 0
    if current_user.is_authenticated:
        cart_count = db.session.query(func.sum(CartItem.quantity)).filter_by(user_id=current_user.id).scalar() or 0
    else:
        cart_count = sum(ci.get('quantity', 1) for ci in session.get('anon_cart', []))
    categories = Category.query.order_by(Category.id).all()
    return {
        'cart_count': cart_count,
        'nav_categories': categories,
        'csrf_token_value': generate_csrf(),
    }


# ----- Forms -----

class LoginForm(FlaskForm):
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired()])


class RegisterForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired(), Length(min=2, max=120)])
    email = StringField('Email', validators=[DataRequired(), Email()])
    password = PasswordField('Password', validators=[DataRequired(), Length(min=6)])
    confirm = PasswordField('Confirm Password', validators=[DataRequired(), EqualTo('password')])


class ProfileForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired()])
    phone = StringField('Phone', validators=[Optional()])
    address_line1 = StringField('Address Line 1', validators=[Optional()])
    address_line2 = StringField('Address Line 2', validators=[Optional()])
    city = StringField('City', validators=[Optional()])
    state = StringField('State', validators=[Optional()])
    zip_code = StringField('ZIP Code', validators=[Optional()])
    country = StringField('Country', validators=[Optional()])


class PasswordForm(FlaskForm):
    current_password = PasswordField('Current Password', validators=[DataRequired()])
    new_password = PasswordField('New Password', validators=[DataRequired(), Length(min=6)])
    confirm = PasswordField('Confirm', validators=[DataRequired(), EqualTo('new_password')])


class CheckoutForm(FlaskForm):
    name = StringField('Name', validators=[DataRequired()])
    address = StringField('Address', validators=[DataRequired()])
    city = StringField('City', validators=[DataRequired()])
    state = StringField('State', validators=[DataRequired()])
    zip_code = StringField('ZIP Code', validators=[DataRequired()])
    phone = StringField('Phone', validators=[Optional()])
    card_number = StringField('Card Number', validators=[DataRequired()])
    card_exp = StringField('Expiration', validators=[DataRequired()])
    card_cvv = StringField('CVV', validators=[DataRequired()])


class ReviewForm(FlaskForm):
    rating = IntegerField('Rating', validators=[DataRequired(), NumberRange(min=1, max=5)])
    title = StringField('Title', validators=[DataRequired(), Length(max=200)])
    body = TextAreaField('Review', validators=[DataRequired()])


class AddressForm(FlaskForm):
    label = SelectField('Address Label', choices=[('Home', 'Home'), ('Work', 'Work'), ('Other', 'Other')])
    full_name = StringField('Full Name', validators=[DataRequired()])
    phone = StringField('Phone', validators=[Optional()])
    address_line1 = StringField('Street Address', validators=[DataRequired()])
    address_line2 = StringField('Apt, Suite, Unit', validators=[Optional()])
    city = StringField('City', validators=[DataRequired()])
    state = StringField('State', validators=[DataRequired()])
    zip_code = StringField('ZIP Code', validators=[DataRequired()])
    country = StringField('Country', validators=[Optional()])


class PaymentForm(FlaskForm):
    card_type = SelectField('Card Type', choices=[('Visa', 'Visa'), ('Mastercard', 'Mastercard'), ('Amex', 'American Express'), ('Discover', 'Discover')])
    card_number = StringField('Card Number', validators=[DataRequired()])
    exp_month = SelectField('Exp Month', choices=[(str(i), f'{i:02d}') for i in range(1, 13)])
    exp_year = SelectField('Exp Year', choices=[(str(i), str(i)) for i in range(2024, 2035)])
    cardholder_name = StringField('Name on Card', validators=[DataRequired()])


# ----- Routes: Static Pages -----

@app.route('/')
def index():
    from sqlalchemy.sql import func as _func
    # Randomize home-page product lists so each refresh shows different items.
    featured = Product.query.filter_by(is_featured=True).order_by(_func.random()).limit(12).all()
    deals = Product.query.filter_by(is_deal=True).order_by(_func.random()).limit(8).all()
    bestsellers = Product.query.filter_by(is_bestseller=True).order_by(_func.random()).limit(12).all()
    electronics = Product.query.filter_by(category_slug='electronics').order_by(_func.random()).limit(6).all()
    fashion = Product.query.filter_by(category_slug='fashion').order_by(_func.random()).limit(6).all()
    home_goods = Product.query.filter_by(category_slug='home').order_by(_func.random()).limit(6).all()
    books = Product.query.filter_by(category_slug='books').order_by(_func.random()).limit(6).all()
    computers = Product.query.filter_by(category_slug='computers').order_by(_func.random()).limit(6).all()
    # Random beauty bestsellers, topped up from other beauty products when fewer than 4 exist.
    beauty = (Product.query.filter_by(category_slug='beauty', is_bestseller=True)
              .order_by(_func.random()).limit(4).all())
    if len(beauty) < 4:
        seen = {p.id for p in beauty}
        q = Product.query.filter_by(category_slug='beauty')
        if seen:
            q = q.filter(~Product.id.in_(seen))
        extra = q.order_by(_func.random()).limit(4 - len(beauty)).all()
        beauty += extra

    resp = make_response(render_template('index.html',
        featured=featured, deals=deals, bestsellers=bestsellers,
        electronics=electronics, fashion=fashion, home_goods=home_goods,
        books=books, computers=computers, beauty=beauty))
    # Prevent browser from caching the home page so each refresh re-shuffles.
    resp.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    resp.headers['Pragma'] = 'no-cache'
    return resp


def _apply_filters(query_obj):
    """Apply request.args filters to a Product query. Used by /search and /c/<slug>."""

    # Price range
    min_price = request.args.get('min_price', type=float)
    max_price = request.args.get('max_price', type=float)
    if min_price is not None:
        query_obj = query_obj.filter(Product.price >= min_price)
    if max_price is not None:
        query_obj = query_obj.filter(Product.price <= max_price)

    # Rating
    min_rating = request.args.get('min_rating', type=float)
    if min_rating is not None:
        query_obj = query_obj.filter(Product.rating >= min_rating)

    # Reviews
    min_reviews = request.args.get('min_reviews', type=int)
    if min_reviews is not None:
        query_obj = query_obj.filter(Product.review_count >= min_reviews)

    # Brand
    brand = request.args.get('brand', '').strip()
    if brand:
        query_obj = query_obj.filter(Product.brand.ilike(f'%{brand}%'))

    # Condition (New, Used - Good, etc.)
    condition = request.args.get('condition', '').strip()
    if condition:
        query_obj = query_obj.filter(Product.condition == condition)

    # Prime
    if request.args.get('prime') == '1':
        query_obj = query_obj.filter(Product.is_prime == True)

    # Deals
    if request.args.get('deal') == '1':
        query_obj = query_obj.filter(Product.is_deal == True)

    # Bestseller
    if request.args.get('bestseller') == '1':
        query_obj = query_obj.filter(Product.is_bestseller == True)

    # Free shipping
    if request.args.get('free_shipping') == '1':
        query_obj = query_obj.filter(Product.free_shipping == True)

    # Free returns
    if request.args.get('free_returns') == '1':
        query_obj = query_obj.filter(Product.free_returns == True)

    # Color (substring match in variant_options JSON)
    color = request.args.get('color', '').strip()
    if color:
        query_obj = query_obj.filter(Product.variant_options.ilike(f'%{color}%'))

    # Size (match any of: exact, normalized, common aliases)
    size = request.args.get('size', '').strip()
    if size:
        from sqlalchemy import or_ as _or
        # Build alias list so "XXL" also matches "XX-Large", "2XL", etc.
        alias_map = {
            'xs': ['xs', 'x-small', 'extra small'],
            's': ['s', 'small'],
            'm': ['m', 'medium'],
            'l': ['l', 'large'],
            'xl': ['xl', 'x-large', 'extra large'],
            'xxl': ['xxl', 'xx-large', '2xl', '2x-large'],
            'xxxl': ['xxxl', 'xxx-large', '3xl', '3x-large'],
        }
        key = size.lower().replace('-', '').replace(' ', '')
        aliases = alias_map.get(key, [size])
        if size not in aliases:
            aliases.append(size)
        conds = [Product.variant_options.ilike(f'%"{a}"%') for a in aliases]
        query_obj = query_obj.filter(_or(*conds))

    # Feature tag (substring match in feature_tags JSON) — split on comma OR space
    feature = request.args.get('feature', '').strip()
    if feature:
        # Split on commas first, then split each chunk on spaces for individual tokens
        import re as _re
        tokens = _re.findall(r'[a-z0-9][\w-]*', feature.lower())
        for tok in tokens:
            if tok and len(tok) >= 2:
                query_obj = query_obj.filter(Product.feature_tags.ilike(f'%{tok}%'))

    # Publication / release year (matches leading 4-digit year in release_date string)
    year = request.args.get('year', '').strip()
    if year and year.isdigit() and len(year) == 4:
        query_obj = query_obj.filter(Product.release_date.ilike(f'%{year}%'))

    return query_obj


def _apply_sort(results, sort_key):
    # Accept multiple naming conventions (price_asc / price_low / low_to_high, etc.)
    key = (sort_key or '').strip().lower()
    if key in ('price_asc', 'price_low', 'price-low', 'low_to_high', 'price-low-to-high', 'lowtohigh', 'low', 'priceasc'):
        return sorted(results, key=lambda p: p.price)
    if key in ('price_desc', 'price_high', 'price-high', 'high_to_low', 'price-high-to-low', 'hightolow', 'high', 'pricedesc'):
        return sorted(results, key=lambda p: p.price, reverse=True)
    if key in ('rating', 'avg_rating', 'avg-rating', 'customer_review', 'review_rating'):
        return sorted(results, key=lambda p: (p.rating, p.review_count), reverse=True)
    if key in ('reviews', 'review_count', 'most_reviews'):
        return sorted(results, key=lambda p: p.review_count, reverse=True)
    if key in ('newest', 'new', 'newest_arrivals', 'release_date'):
        return sorted(results, key=lambda p: p.created_at, reverse=True)
    if key in ('bestseller', 'best_sellers', 'best-sellers', 'popular'):
        return sorted(results, key=lambda p: (p.is_bestseller, p.review_count), reverse=True)
    return results  # featured / default — keep DB order


def _normalize_sort_key(sort_key):
    """Map any alias to the canonical value used in the dropdown <option> tags."""
    key = (sort_key or '').strip().lower()
    if key in ('price_asc', 'price_low', 'price-low', 'low_to_high', 'price-low-to-high', 'lowtohigh', 'low', 'priceasc'):
        return 'price_asc'
    if key in ('price_desc', 'price_high', 'price-high', 'high_to_low', 'price-high-to-low', 'hightolow', 'high', 'pricedesc'):
        return 'price_desc'
    if key in ('rating', 'avg_rating', 'avg-rating', 'customer_review', 'review_rating'):
        return 'rating'
    if key in ('reviews', 'review_count', 'most_reviews'):
        return 'reviews'
    if key in ('newest', 'new', 'newest_arrivals', 'release_date'):
        return 'newest'
    if key in ('bestseller', 'best_sellers', 'best-sellers', 'popular'):
        return 'bestseller'
    return ''


@app.route('/c/<slug>')
def category(slug):
    cat = Category.query.filter_by(slug=slug).first_or_404()
    q = Product.query.filter_by(category_slug=slug)
    sub = request.args.get('sub', '')
    if sub:
        q = q.filter(Product.subcategory == sub)
    q = _apply_filters(q)
    products = q.all()
    sort_raw = request.args.get('sort', '')
    products = _apply_sort(products, sort_raw)
    current_sort = _normalize_sort_key(sort_raw)

    all_products = Product.query.filter_by(category_slug=slug).all()
    subcats = sorted({p.subcategory for p in all_products if p.subcategory})
    return render_template('category.html', category=cat, products=products,
                           subcategories=subcats, current_sub=sub, current_sort=current_sort)


@app.route('/product/<slug>')
def product_detail(slug):
    product = Product.query.filter_by(slug=slug).first_or_404()
    related = Product.query.filter(
        Product.category_slug == product.category_slug,
        Product.id != product.id
    ).limit(8).all()
    all_reviews = Review.query.filter_by(product_id=product.id).order_by(Review.created_at.desc()).all()
    # Top review = highest-rated, then most recent among those
    top_review = None
    if all_reviews:
        top_review = sorted(all_reviews, key=lambda r: (r.rating, r.created_at), reverse=True)[0]
    # Limit displayed reviews to the top 10 most-recent (DBs hold a sampled 12-30 per product)
    reviews = all_reviews[:10]
    in_wishlist = False
    if current_user.is_authenticated:
        in_wishlist = WishlistItem.query.filter_by(user_id=current_user.id, product_id=product.id).first() is not None
    return render_template('product_detail.html',
        product=product, related=related, reviews=reviews, top_review=top_review,
        in_wishlist=in_wishlist, review_form=ReviewForm())


STOPWORDS = {'the', 'a', 'an', 'of', 'in', 'on', 'at', 'to', 'for', 'with',
             'and', 'or', 'is', 'are', 'be', 'by', 'from', 'as', 'that', 'this',
             'mens', 'womens', 'men', 'women', 'kids', 'boys', 'girls'}
# Gender words are stopwords so substring matching never confuses "mens" with "womens".


def _score_product(product, tokens):
    """Number of distinct search tokens that match the product's tokenised text."""
    raw = ' '.join([
        (product.name or ''),
        (product.brand or ''),
        (product.description or ''),
        (product.category_slug or ''),
        (product.subcategory or ''),
        (product.feature_tags or ''),
        (product.specs or ''),
    ]).lower()
    hay_tokens = set(re.findall(r'[a-z0-9]+', raw))
    hay_raw = raw  # for substring fallback ("phone" inside "iphone")

    def _match(t):
        if t in hay_tokens:
            return True
        # Plural/singular tolerance ('polos' <-> 'polo') for tokens longer than 3 chars.
        if len(t) > 3:
            if t.endswith('es') and t[:-2] in hay_tokens:
                return True
            if t.endswith('s') and t[:-1] in hay_tokens:
                return True
            if (t + 's') in hay_tokens:
                return True
            if (t + 'es') in hay_tokens:
                return True
        # Prefix match for partial queries ("watc" -> "watch"), bounded so short prefixes don't over-match.
        if len(t) >= 3:
            for hw in hay_tokens:
                if hw.startswith(t) and len(hw) <= len(t) * 2 + 3:
                    return True
        # Substring fallback ("phone" in "iphone") for tokens of 4+ chars.
        if len(t) >= 4 and t in hay_raw:
            return True
        return False

    distinct = sum(1 for t in tokens if _match(t))
    return distinct


@app.route('/search')
def search():
    q = request.args.get('q', '').strip()
    query_obj = Product.query
    query_obj = _apply_filters(query_obj)  # apply structural filters first
    candidates = query_obj.all()

    if q:
        # Tokenize: lowercase, drop stopwords, drop very short tokens
        tokens = [t.lower() for t in re.findall(r'[a-z0-9]+', q.lower())
                  if t and t not in STOPWORDS and len(t) >= 2]
        if tokens:
            # Require a majority of query tokens so single-token matches don't pad the results.
            if len(tokens) >= 5:
                min_required = (len(tokens) // 2) + 1            # e.g. 5->3, 6->4, 7->4
            elif len(tokens) >= 3:
                min_required = max(2, (len(tokens) + 1) // 2)    # e.g. 3->2, 4->2
            elif len(tokens) == 2:
                min_required = 2
            else:
                min_required = 1
            scored = []
            for p in candidates:
                s = _score_product(p, tokens)
                if s >= min_required:
                    scored.append((s, p))
            # Shuffle within each score tier, seeded by the query, so the top match is not always first.
            query_seed = abs(hash(q.lower())) % (2**31)
            rng = random.Random(query_seed)
            # Group by score, shuffle each group, then reassemble in score desc
            from itertools import groupby
            scored.sort(key=lambda x: -x[0])
            results = []
            for _, group in groupby(scored, key=lambda x: x[0]):
                bucket = [p for _, p in group]
                rng.shuffle(bucket)
                results.extend(bucket)
            # No padding with unrelated products; fewer (or zero) matches are shown as-is.
        else:
            results = candidates
    else:
        results = candidates

    sort_raw = request.args.get('sort', '')
    results = _apply_sort(results, sort_raw)
    current_sort = _normalize_sort_key(sort_raw)
    return render_template('search.html', query=q, results=results, current_sort=current_sort)


@app.route('/deals')
def deals():
    products = Product.query.filter_by(is_deal=True).all()
    return render_template('deals.html', products=products)


@app.route('/bestsellers')
def bestsellers():
    products = Product.query.filter_by(is_bestseller=True).all()
    return render_template('bestsellers.html', products=products)


@app.route('/prime')
def prime():
    products = Product.query.filter_by(is_prime=True).limit(30).all()
    return render_template('prime.html', products=products)


@app.route('/customer-service')
def customer_service():
    return render_template('customer_service.html')


@app.route('/gift-cards')
def gift_cards():
    return render_template('gift_cards.html')


@app.route('/todays-deals')
def todays_deals():
    return redirect(url_for('deals'))


# ----- Routes: Auth -----

@app.route('/login', methods=['GET', 'POST'])
@csrf.exempt
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = LoginForm()
    if form.validate_on_submit():
        user = User.query.filter_by(email=form.email.data.lower().strip()).first()
        if user and user.check_password(form.password.data):
            login_user(user)
            flash(f'Welcome back, {user.name}!', 'success')
            next_url = request.args.get('next')
            # Only internal GET-safe next targets; the POST-only /cart/add falls back to the bag.
            if next_url:
                # Only allow relative/internal paths (no scheme/host)
                if not next_url.startswith('/') or next_url.startswith('//'):
                    next_url = None
                elif '/cart/add' in next_url:
                    # POST-only route; redirect to the cart page instead
                    next_url = url_for('bag')
            return redirect(next_url or url_for('index'))
        flash('Invalid email or password.', 'error')
    return render_template('login.html', form=form)


@app.route('/register', methods=['GET', 'POST'])
@csrf.exempt
def register():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    form = RegisterForm()
    if form.validate_on_submit():
        email = form.email.data.lower().strip()
        if User.query.filter_by(email=email).first():
            flash('That email is already registered.', 'error')
        else:
            user = User(email=email, name=form.name.data.strip())
            user.set_password(form.password.data)
            db.session.add(user)
            db.session.commit()
            login_user(user)
            flash('Welcome to Amazon!', 'success')
            return redirect(url_for('index'))
    return render_template('register.html', form=form)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been signed out.', 'info')
    return redirect(url_for('index'))


# ----- Routes: Account -----

@app.route('/account')
@login_required
def account():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).limit(5).all()
    wishlist_count = WishlistItem.query.filter_by(user_id=current_user.id).count()
    return render_template('account.html', orders=orders, wishlist_count=wishlist_count)


@app.route('/account/orders')
@login_required
def orders_list():
    orders = Order.query.filter_by(user_id=current_user.id).order_by(Order.created_at.desc()).all()
    return render_template('orders.html', orders=orders)


@app.route('/account/edit', methods=['GET', 'POST'])
@csrf.exempt
@login_required
def edit_profile():
    form = ProfileForm(obj=current_user)
    if form.validate_on_submit():
        current_user.name = form.name.data
        current_user.phone = form.phone.data or ''
        current_user.address_line1 = form.address_line1.data or ''
        current_user.address_line2 = form.address_line2.data or ''
        current_user.city = form.city.data or ''
        current_user.state = form.state.data or ''
        current_user.zip_code = form.zip_code.data or ''
        current_user.country = form.country.data or 'United States'
        db.session.commit()
        flash('Your profile has been updated.', 'success')
        return redirect(url_for('account'))
    return render_template('account_edit.html', form=form)


@app.route('/account/password', methods=['GET', 'POST'])
@csrf.exempt
@login_required
def change_password():
    form = PasswordForm()
    if form.validate_on_submit():
        if not current_user.check_password(form.current_password.data):
            flash('Current password is incorrect.', 'error')
        else:
            current_user.set_password(form.new_password.data)
            db.session.commit()
            flash('Your password has been changed.', 'success')
            return redirect(url_for('account'))
    return render_template('change_password.html', form=form)


@app.route('/account/delete', methods=['POST'])
@csrf.exempt
@login_required
def delete_account():
    user = current_user._get_current_object()
    logout_user()
    db.session.delete(user)
    db.session.commit()
    flash('Your account has been deleted.', 'info')
    return redirect(url_for('index'))


# ----- Routes: Saved Addresses -----

@app.route('/account/addresses')
@login_required
def addresses_list():
    addrs = SavedAddress.query.filter_by(user_id=current_user.id).order_by(SavedAddress.is_default.desc(), SavedAddress.created_at).all()
    return render_template('addresses.html', addresses=addrs)


@app.route('/account/addresses/add', methods=['GET', 'POST'])
@csrf.exempt
@login_required
def address_add():
    form = AddressForm()
    if form.validate_on_submit():
        addr = SavedAddress(
            user_id=current_user.id,
            label=form.label.data,
            full_name=form.full_name.data,
            phone=form.phone.data or '',
            address_line1=form.address_line1.data,
            address_line2=form.address_line2.data or '',
            city=form.city.data,
            state=form.state.data,
            zip_code=form.zip_code.data,
            country=form.country.data or 'United States',
            is_default=SavedAddress.query.filter_by(user_id=current_user.id).count() == 0,
        )
        db.session.add(addr)
        db.session.commit()
        flash('Address added.', 'success')
        return redirect(url_for('addresses_list'))
    return render_template('address_form.html', form=form, editing=False)


@app.route('/account/addresses/<int:addr_id>/edit', methods=['GET', 'POST'])
@csrf.exempt
@login_required
def address_edit(addr_id):
    addr = SavedAddress.query.filter_by(id=addr_id, user_id=current_user.id).first_or_404()
    form = AddressForm(obj=addr)
    if form.validate_on_submit():
        addr.label = form.label.data
        addr.full_name = form.full_name.data
        addr.phone = form.phone.data or ''
        addr.address_line1 = form.address_line1.data
        addr.address_line2 = form.address_line2.data or ''
        addr.city = form.city.data
        addr.state = form.state.data
        addr.zip_code = form.zip_code.data
        addr.country = form.country.data or 'United States'
        db.session.commit()
        flash('Address updated.', 'success')
        return redirect(url_for('addresses_list'))
    return render_template('address_form.html', form=form, editing=True, addr=addr)


@app.route('/account/addresses/<int:addr_id>/delete', methods=['POST'])
@csrf.exempt
@login_required
def address_delete(addr_id):
    addr = SavedAddress.query.filter_by(id=addr_id, user_id=current_user.id).first_or_404()
    was_default = addr.is_default
    db.session.delete(addr)
    if was_default:
        next_addr = SavedAddress.query.filter_by(user_id=current_user.id).first()
        if next_addr:
            next_addr.is_default = True
    db.session.commit()
    flash('Address removed.', 'success')
    return redirect(url_for('addresses_list'))


@app.route('/account/addresses/<int:addr_id>/set-default', methods=['POST'])
@csrf.exempt
@login_required
def address_set_default(addr_id):
    addr = SavedAddress.query.filter_by(id=addr_id, user_id=current_user.id).first_or_404()
    SavedAddress.query.filter_by(user_id=current_user.id).update({'is_default': False})
    addr.is_default = True
    db.session.commit()
    flash(f'{addr.label} address set as default.', 'success')
    return redirect(url_for('addresses_list'))


# ----- Routes: Payment Methods -----

@app.route('/account/payment')
@login_required
def payment_list():
    methods = PaymentMethod.query.filter_by(user_id=current_user.id).order_by(PaymentMethod.is_default.desc(), PaymentMethod.created_at).all()
    return render_template('payment.html', methods=methods)


@app.route('/account/payment/add', methods=['GET', 'POST'])
@csrf.exempt
@login_required
def payment_add():
    form = PaymentForm()
    if form.validate_on_submit():
        pm = PaymentMethod(
            user_id=current_user.id,
            card_type=form.card_type.data,
            last4=form.card_number.data[-4:],
            exp_month=int(form.exp_month.data),
            exp_year=int(form.exp_year.data),
            cardholder_name=form.cardholder_name.data,
            is_default=PaymentMethod.query.filter_by(user_id=current_user.id).count() == 0,
        )
        db.session.add(pm)
        db.session.commit()
        flash('Payment method added.', 'success')
        return redirect(url_for('payment_list'))
    return render_template('payment_form.html', form=form)


@app.route('/account/payment/<int:pm_id>/remove', methods=['POST'])
@csrf.exempt
@login_required
def payment_remove(pm_id):
    pm = PaymentMethod.query.filter_by(id=pm_id, user_id=current_user.id).first_or_404()
    was_default = pm.is_default
    db.session.delete(pm)
    if was_default:
        next_pm = PaymentMethod.query.filter_by(user_id=current_user.id).first()
        if next_pm:
            next_pm.is_default = True
    db.session.commit()
    flash('Payment method removed.', 'success')
    return redirect(url_for('payment_list'))


@app.route('/account/payment/<int:pm_id>/set-default', methods=['POST'])
@csrf.exempt
@login_required
def payment_set_default(pm_id):
    pm = PaymentMethod.query.filter_by(id=pm_id, user_id=current_user.id).first_or_404()
    PaymentMethod.query.filter_by(user_id=current_user.id).update({'is_default': False})
    pm.is_default = True
    db.session.commit()
    flash(f'{pm.card_type} ending in {pm.last4} set as default.', 'success')
    return redirect(url_for('payment_list'))


# ----- Routes: Cart / Bag -----

def _anon_cart_items():
    """Return list of dict-like items for session cart (anonymous users)."""
    items = []
    for ci in session.get('anon_cart', []):
        product = Product.query.get(ci.get('product_id'))
        if product:
            items.append({
                'id': ci.get('id', 0),
                'product': product,
                'quantity': ci.get('quantity', 1),
                'variant': ci.get('variant', ''),
            })
    return items


def _anon_cart_count():
    return sum(ci.get('quantity', 1) for ci in session.get('anon_cart', []))


class _AnonItem:
    """Duck-typed wrapper so bag.html loops can treat session items like CartItem objects."""
    def __init__(self, d):
        self.id = d['id']
        self.product = d['product']
        self.quantity = d['quantity']
        self.variant = d['variant']


@app.route('/bag')
def bag():
    if current_user.is_authenticated:
        items = CartItem.query.filter_by(user_id=current_user.id).all()
    else:
        items = [_AnonItem(d) for d in _anon_cart_items()]
    subtotal = sum(i.product.price * i.quantity for i in items)
    return render_template('bag.html', items=items, subtotal=subtotal)


@app.route('/cart')
def cart():
    """Alias for /bag - Amazon-style cart page."""
    return redirect(url_for('bag'))


@app.route('/cart/add/<int:product_id>', methods=['POST'])
@csrf.exempt
def cart_add_form(product_id):
    """Non-AJAX add-to-cart via form POST, for logged-in (DB cart) and anonymous (session cart) users."""
    product = Product.query.get_or_404(product_id)
    variant = request.form.get('variant', '')
    qty = int(request.form.get('quantity', 1))
    if current_user.is_authenticated:
        existing = CartItem.query.filter_by(
            user_id=current_user.id, product_id=product_id, variant=variant
        ).first()
        if existing:
            existing.quantity += qty
        else:
            db.session.add(CartItem(user_id=current_user.id, product_id=product_id, quantity=qty, variant=variant))
        db.session.commit()
    else:
        cart = session.get('anon_cart', [])
        found = False
        for ci in cart:
            if ci.get('product_id') == product_id and ci.get('variant', '') == variant:
                ci['quantity'] += qty
                found = True
                break
        if not found:
            next_id = max([ci.get('id', 0) for ci in cart] or [0]) + 1
            cart.append({'id': next_id, 'product_id': product_id, 'quantity': qty, 'variant': variant})
        session['anon_cart'] = cart
        session.modified = True
    flash(f'Added {product.name} to cart. Item added to your shopping cart.', 'success')
    next_url = request.form.get('next', '')
    if next_url == 'checkout':
        return redirect(url_for('checkout'))
    return redirect(url_for('bag'))


@app.route('/api/cart/add', methods=['POST'])
@csrf.exempt
def cart_add():
    if not current_user.is_authenticated:
        return jsonify({'success': False, 'error': 'Please sign in', 'redirect': url_for('login')}), 401
    data = request.get_json() or {}
    pid = data.get('product_id')
    qty = int(data.get('quantity', 1))
    variant = data.get('variant', '')
    product = Product.query.get(pid)
    if not product:
        return jsonify({'success': False, 'error': 'Product not found'}), 404

    existing = CartItem.query.filter_by(
        user_id=current_user.id, product_id=pid, variant=variant
    ).first()
    if existing:
        existing.quantity += qty
    else:
        db.session.add(CartItem(user_id=current_user.id, product_id=pid, quantity=qty, variant=variant))
    db.session.commit()

    cart_count = db.session.query(func.sum(CartItem.quantity)).filter_by(user_id=current_user.id).scalar() or 0
    return jsonify({'success': True, 'cart_count': int(cart_count), 'message': f'Added {product.name} to cart'})


@app.route('/api/cart/update', methods=['POST'])
@csrf.exempt
@login_required
def cart_update():
    data = request.get_json() or {}
    item_id = data.get('item_id')
    qty = int(data.get('quantity', 1))
    item = CartItem.query.filter_by(id=item_id, user_id=current_user.id).first()
    if not item:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if qty <= 0:
        db.session.delete(item)
    else:
        item.quantity = qty
    db.session.commit()

    items = CartItem.query.filter_by(user_id=current_user.id).all()
    subtotal = sum(i.product.price * i.quantity for i in items)
    cart_count = sum(i.quantity for i in items)
    return jsonify({'success': True, 'cart_count': cart_count, 'subtotal': round(subtotal, 2)})


@app.route('/api/cart/remove', methods=['POST'])
@csrf.exempt
@login_required
def cart_remove():
    data = request.get_json() or {}
    item_id = data.get('item_id')
    item = CartItem.query.filter_by(id=item_id, user_id=current_user.id).first()
    if not item:
        return jsonify({'success': False}), 404
    db.session.delete(item)
    db.session.commit()
    items = CartItem.query.filter_by(user_id=current_user.id).all()
    subtotal = sum(i.product.price * i.quantity for i in items)
    cart_count = sum(i.quantity for i in items)
    return jsonify({'success': True, 'cart_count': cart_count, 'subtotal': round(subtotal, 2)})


# ----- Routes: Checkout / Orders -----

@app.route('/checkout', methods=['GET', 'POST'])
@csrf.exempt
@login_required
def checkout():
    items = CartItem.query.filter_by(user_id=current_user.id).all()
    if not items:
        flash('Your cart is empty.', 'info')
        return redirect(url_for('bag'))

    subtotal = sum(i.product.price * i.quantity for i in items)
    shipping = 0 if current_user.is_prime or subtotal > 35 else 5.99
    tax = round(subtotal * 0.0825, 2)
    total = round(subtotal + shipping + tax, 2)

    saved_addrs = SavedAddress.query.filter_by(user_id=current_user.id).order_by(SavedAddress.is_default.desc()).all()
    saved_payments = PaymentMethod.query.filter_by(user_id=current_user.id).order_by(PaymentMethod.is_default.desc()).all()

    form = CheckoutForm()
    # Pre-fill from default saved address or user profile
    if request.method == 'GET':
        default_addr = next((a for a in saved_addrs if a.is_default), None)
        if default_addr:
            form.name.data = default_addr.full_name
            form.address.data = default_addr.address_line1
            form.city.data = default_addr.city
            form.state.data = default_addr.state
            form.zip_code.data = default_addr.zip_code
            form.phone.data = default_addr.phone
        else:
            form.name.data = current_user.name
            form.address.data = current_user.address_line1
            form.city.data = current_user.city
            form.state.data = current_user.state
            form.zip_code.data = current_user.zip_code
            form.phone.data = current_user.phone

    if form.validate_on_submit():
        # Determine payment info from saved method or form entry
        selected_pm_id = request.form.get('saved_payment_id')
        if selected_pm_id and selected_pm_id != 'new':
            pm = PaymentMethod.query.filter_by(id=int(selected_pm_id), user_id=current_user.id).first()
            pay_method = pm.card_type if pm else 'Visa'
            pay_last4 = pm.last4 if pm else '1234'
        else:
            pay_method = 'Visa'
            pay_last4 = form.card_number.data[-4:] if form.card_number.data else '1234'

        # Determine shipping address from saved address or form entry
        selected_addr_id = request.form.get('saved_address_id')
        if selected_addr_id and selected_addr_id != 'new':
            addr = SavedAddress.query.filter_by(id=int(selected_addr_id), user_id=current_user.id).first()
            if addr:
                ship_name = addr.full_name
                ship_address = addr.address_line1
                ship_city = addr.city
                ship_state = addr.state
                ship_zip = addr.zip_code
            else:
                ship_name, ship_address, ship_city, ship_state, ship_zip = (
                    form.name.data, form.address.data, form.city.data, form.state.data, form.zip_code.data)
        else:
            ship_name = form.name.data
            ship_address = form.address.data
            ship_city = form.city.data
            ship_state = form.state.data
            ship_zip = form.zip_code.data

        order_num = f"112-{random.randint(1000000, 9999999)}-{random.randint(1000000, 9999999)}"
        order = Order(
            user_id=current_user.id,
            order_number=order_num,
            status='processing',
            subtotal=round(subtotal, 2),
            shipping=shipping,
            tax=tax,
            total=total,
            ship_name=ship_name,
            ship_address=ship_address,
            ship_city=ship_city,
            ship_state=ship_state,
            ship_zip=ship_zip,
            payment_method=pay_method,
            payment_last4=pay_last4,
            delivery_estimate=(datetime.utcnow() + timedelta(days=random.randint(2, 5))).strftime('%A, %B %d')
        )
        db.session.add(order)
        db.session.flush()

        for item in items:
            db.session.add(OrderItem(
                order_id=order.id,
                product_id=item.product_id,
                product_name=item.product.name,
                product_image=item.product.image,
                variant=item.variant,
                quantity=item.quantity,
                price=item.product.price
            ))
            db.session.delete(item)

        db.session.commit()
        return redirect(url_for('order_confirmation', order_id=order.id))

    return render_template('checkout.html', form=form, items=items,
        subtotal=subtotal, shipping=shipping, tax=tax, total=total,
        saved_addrs=saved_addrs, saved_payments=saved_payments)


@app.route('/order/<int:order_id>/confirmation')
@login_required
def order_confirmation(order_id):
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
    return render_template('order_confirmation.html', order=order)


@app.route('/order/<int:order_id>')
@login_required
def order_detail(order_id):
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
    return render_template('order_detail.html', order=order)


@app.route('/order/<int:order_id>/cancel', methods=['POST'])
@csrf.exempt
@login_required
def order_cancel(order_id):
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
    if order.status == 'processing':
        order.status = 'cancelled'
        db.session.commit()
        flash('Order cancelled.', 'success')
    else:
        flash('Only processing orders can be cancelled.', 'error')
    return redirect(url_for('order_detail', order_id=order.id))


@app.route('/order/<int:order_id>/reorder', methods=['POST'])
@csrf.exempt
@login_required
def order_reorder(order_id):
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
    count = 0
    for oi in order.items:
        existing = CartItem.query.filter_by(
            user_id=current_user.id, product_id=oi.product_id, variant=oi.variant
        ).first()
        if existing:
            existing.quantity += oi.quantity
        else:
            db.session.add(CartItem(
                user_id=current_user.id, product_id=oi.product_id,
                quantity=oi.quantity, variant=oi.variant
            ))
        count += 1
    db.session.commit()
    flash(f'{count} item(s) added back to your cart.', 'success')
    return redirect(url_for('bag'))


# ----- Routes: Returns -----

RETURN_REASONS = [
    ('doesnt_fit', "Doesn't fit"),
    ('defective', 'Item defective or doesn\'t work'),
    ('changed_mind', 'No longer needed'),
    ('wrong_item', 'Wrong item was sent'),
    ('better_price', 'Better price available'),
    ('damaged', 'Product damaged on arrival'),
]

@app.route('/order/<int:order_id>/return', methods=['GET', 'POST'])
@csrf.exempt
@login_required
def order_return(order_id):
    order = Order.query.filter_by(id=order_id, user_id=current_user.id).first_or_404()
    if order.status != 'delivered':
        flash('Only delivered orders can be returned.', 'error')
        return redirect(url_for('order_detail', order_id=order.id))
    # Check 30-day return window
    if order.created_at and (datetime.utcnow() - order.created_at).days > 30:
        flash('Return window (30 days) has expired for this order.', 'error')
        return redirect(url_for('order_detail', order_id=order.id))

    if request.method == 'POST':
        selected_item_ids = request.form.getlist('return_items')
        if not selected_item_ids:
            flash('Please select at least one item to return.', 'error')
            return render_template('return.html', order=order, reasons=RETURN_REASONS)

        refund_method = request.form.get('refund_method', 'original_payment')
        refund_amount = 0

        ret = Return(
            order_id=order.id,
            user_id=current_user.id,
            status='requested',
            refund_method=refund_method,
        )
        db.session.add(ret)
        db.session.flush()

        for oi_id in selected_item_ids:
            oi = OrderItem.query.get(int(oi_id))
            if oi and oi.order_id == order.id:
                reason = request.form.get(f'reason_{oi_id}', 'changed_mind')
                qty = int(request.form.get(f'qty_{oi_id}', oi.quantity))
                refund_amount += oi.price * qty
                db.session.add(ReturnItem(
                    return_id=ret.id,
                    order_item_id=oi.id,
                    product_name=oi.product_name,
                    quantity=qty,
                    reason=reason,
                ))

        ret.refund_amount = round(refund_amount, 2)
        db.session.commit()
        return redirect(url_for('return_confirmation', return_id=ret.id))

    return render_template('return.html', order=order, reasons=RETURN_REASONS)


@app.route('/return/<int:return_id>/confirmation')
@login_required
def return_confirmation(return_id):
    ret = Return.query.filter_by(id=return_id, user_id=current_user.id).first_or_404()
    return render_template('return_confirmation.html', ret=ret)


# ----- Routes: Wishlist -----

@app.route('/wishlist')
@login_required
def wishlist():
    items = WishlistItem.query.filter_by(user_id=current_user.id).order_by(WishlistItem.added_at.desc()).all()
    return render_template('wishlist.html', items=items)


@app.route('/api/wishlist/toggle', methods=['POST'])
@csrf.exempt
def wishlist_toggle():
    if not current_user.is_authenticated:
        return jsonify({'success': False, 'error': 'Please sign in', 'redirect': url_for('login')}), 401
    data = request.get_json() or {}
    pid = data.get('product_id')
    existing = WishlistItem.query.filter_by(user_id=current_user.id, product_id=pid).first()
    if existing:
        db.session.delete(existing)
        db.session.commit()
        return jsonify({'success': True, 'in_wishlist': False, 'message': 'Removed from wishlist'})
    else:
        db.session.add(WishlistItem(user_id=current_user.id, product_id=pid))
        db.session.commit()
        return jsonify({'success': True, 'in_wishlist': True, 'message': 'Added to wishlist'})


@app.route('/wishlist/remove/<int:item_id>', methods=['POST'])
@csrf.exempt
@login_required
def wishlist_remove(item_id):
    item = WishlistItem.query.filter_by(id=item_id, user_id=current_user.id).first_or_404()
    db.session.delete(item)
    db.session.commit()
    flash('Removed from wishlist.', 'info')
    return redirect(url_for('wishlist'))


# ----- Routes: Reviews -----

@app.route('/product/<slug>/review', methods=['POST'])
@csrf.exempt
@login_required
def add_review(slug):
    product = Product.query.filter_by(slug=slug).first_or_404()
    form = ReviewForm()
    if form.validate_on_submit():
        review = Review(
            user_id=current_user.id,
            product_id=product.id,
            rating=form.rating.data,
            title=form.title.data,
            body=form.body.data
        )
        db.session.add(review)
        # Update product avg rating
        reviews = Review.query.filter_by(product_id=product.id).all()
        if reviews:
            product.review_count = len(reviews) + 1
            total = sum(r.rating for r in reviews) + form.rating.data
            product.rating = round(total / product.review_count, 1)
        db.session.commit()
        flash('Thanks for your review!', 'success')
    else:
        flash('Please fill all fields.', 'error')
    return redirect(url_for('product_detail', slug=slug))


@app.route('/review/<int:review_id>/delete', methods=['POST'])
@csrf.exempt
@login_required
def delete_review(review_id):
    review = Review.query.filter_by(id=review_id, user_id=current_user.id).first_or_404()
    slug = review.product.slug
    db.session.delete(review)
    db.session.commit()
    flash('Review deleted.', 'info')
    return redirect(url_for('product_detail', slug=slug))


# ----- API -----

@app.route('/api/products/<category_slug>')
def api_products(category_slug):
    products = Product.query.filter_by(category_slug=category_slug).limit(50).all()
    return jsonify([{
        'id': p.id, 'name': p.name, 'slug': p.slug, 'price': p.price,
        'image': p.image, 'rating': p.rating, 'review_count': p.review_count
    } for p in products])


# ----- Error handlers -----

@app.errorhandler(404)
def not_found(e):
    return render_template('404.html'), 404


@app.errorhandler(500)
def server_error(e):
    db.session.rollback()
    return render_template('500.html'), 500


# ----- Seed Data -----

def seed_database():
    """Populate the database with categories, products, reviews."""
    from seed_data import run_seed
    run_seed(db, Category, Product, User, Review)


def seed_benchmark_users():
    """Seed users with addresses, payment methods, orders and a cart; run after the full catalog is loaded."""
    if User.query.filter_by(email='alice.j@test.com').first():
        return  # already seeded

    users_data = [
        {'name': 'Alice Johnson', 'email': 'alice.j@test.com', 'password': 'TestPass123!', 'is_prime': True,
         'phone': '415-555-0101', 'city': 'San Francisco', 'state': 'CA', 'zip_code': '94102',
         'address_line1': '456 Oak Ave'},
        {'name': 'Bob Chen', 'email': 'bob.c@test.com', 'password': 'TestPass123!', 'is_prime': False,
         'phone': '512-555-0202', 'city': 'Austin', 'state': 'TX', 'zip_code': '78701',
         'address_line1': '789 Pine St'},
        {'name': 'Carol Davis', 'email': 'carol.d@test.com', 'password': 'TestPass123!', 'is_prime': True,
         'phone': '212-555-0303', 'city': 'New York', 'state': 'NY', 'zip_code': '10001',
         'address_line1': '321 Broadway'},
        {'name': 'David Kim', 'email': 'david.k@test.com', 'password': 'TestPass123!', 'is_prime': True,
         'phone': '312-555-0404', 'city': 'Chicago', 'state': 'IL', 'zip_code': '60601',
         'address_line1': '555 Michigan Ave'},
    ]

    created_users = []
    for ud in users_data:
        u = User(email=ud['email'], name=ud['name'], phone=ud['phone'],
                 address_line1=ud['address_line1'], city=ud['city'], state=ud['state'],
                 zip_code=ud['zip_code'], is_prime=ud['is_prime'])
        u.set_password(ud['password'])
        db.session.add(u)
        db.session.flush()
        created_users.append(u)

    alice, bob, carol, david = created_users

    # --- Saved Addresses ---
    addresses_data = [
        # Alice: 2 addresses, Home (default) + Work
        (alice, 'Home', 'Alice Johnson', '415-555-0101', '456 Oak Ave', '', 'San Francisco', 'CA', '94102', True),
        (alice, 'Work', 'Alice Johnson', '415-555-0199', '100 Market St, Floor 5', '', 'San Francisco', 'CA', '94105', False),
        # Bob: 1 address
        (bob, 'Home', 'Bob Chen', '512-555-0202', '789 Pine St', 'Apt 4B', 'Austin', 'TX', '78701', True),
        # Carol: 3 addresses — for gift shopping tasks
        (carol, 'Home', 'Carol Davis', '212-555-0303', '321 Broadway', 'Apt 12A', 'New York', 'NY', '10001', True),
        (carol, 'Work', 'Carol Davis', '212-555-0388', '200 Park Ave', 'Suite 1500', 'New York', 'NY', '10166', False),
        (carol, 'Other', 'Sarah Johnson', '217-555-0777', '742 Evergreen Terrace', '', 'Springfield', 'IL', '62701', False),
        # David: 3 addresses
        (david, 'Home', 'David Kim', '312-555-0404', '555 Michigan Ave', '', 'Chicago', 'IL', '60601', True),
        (david, 'Work', 'David Kim', '312-555-0455', '233 S Wacker Dr', 'Floor 40', 'Chicago', 'IL', '60606', False),
        (david, 'Other', 'Lisa Kim', '847-555-0622', '1500 Sheridan Rd', 'Apt 3C', 'Evanston', 'IL', '60201', False),
    ]
    for user, label, name, phone, a1, a2, city, state, zc, default in addresses_data:
        db.session.add(SavedAddress(
            user_id=user.id, label=label, full_name=name, phone=phone,
            address_line1=a1, address_line2=a2, city=city, state=state,
            zip_code=zc, is_default=default))

    # --- Payment Methods ---
    # Alice gets 2 Visas (for ambiguity testing) + 1 Mastercard
    payments_data = [
        (alice, 'Visa', '4242', 12, 2027, 'Alice Johnson', True),
        (alice, 'Visa', '1177', 6, 2028, 'Alice Johnson', False),
        (alice, 'Mastercard', '8891', 3, 2026, 'Alice Johnson', False),
        (bob, 'Visa', '5678', 9, 2027, 'Bob Chen', True),
        (carol, 'Amex', '3456', 11, 2028, 'Carol Davis', True),
        (carol, 'Visa', '9012', 5, 2026, 'Carol Davis', False),
        (carol, 'Mastercard', '6677', 3, 2027, 'Carol Davis', False),
        (david, 'Mastercard', '7734', 8, 2027, 'David Kim', True),
        (david, 'Discover', '4455', 2, 2028, 'David Kim', False),
    ]
    for user, ctype, last4, em, ey, name, default in payments_data:
        db.session.add(PaymentMethod(
            user_id=user.id, card_type=ctype, last4=last4,
            exp_month=em, exp_year=ey, cardholder_name=name, is_default=default))

    # --- Pre-seeded Orders ---
    # We need products from DB for order items
    def _get_product(name_fragment):
        return Product.query.filter(Product.name.ilike(f'%{name_fragment}%')).first()

    def _make_order(user, status, items_spec, days_ago):
        order_num = f"112-{random.randint(1000000, 9999999)}-{random.randint(1000000, 9999999)}"
        subtotal = sum(p.price * qty for p, qty, _ in items_spec)
        ship_cost = 0 if user.is_prime or subtotal > 35 else 5.99
        tax = round(subtotal * 0.0825, 2)
        total = round(subtotal + ship_cost + tax, 2)
        default_addr = SavedAddress.query.filter_by(user_id=user.id, is_default=True).first()
        default_pm = PaymentMethod.query.filter_by(user_id=user.id, is_default=True).first()
        order = Order(
            user_id=user.id, order_number=order_num, status=status,
            subtotal=round(subtotal, 2), shipping=ship_cost, tax=tax, total=total,
            ship_name=default_addr.full_name if default_addr else user.name,
            ship_address=default_addr.address_line1 if default_addr else user.address_line1,
            ship_city=default_addr.city if default_addr else user.city,
            ship_state=default_addr.state if default_addr else user.state,
            ship_zip=default_addr.zip_code if default_addr else user.zip_code,
            payment_method=default_pm.card_type if default_pm else 'Visa',
            payment_last4=default_pm.last4 if default_pm else '1234',
            created_at=datetime.utcnow() - timedelta(days=days_ago),
            delivery_estimate=(datetime.utcnow() + timedelta(days=random.randint(2, 5))).strftime('%A, %B %d'),
        )
        db.session.add(order)
        db.session.flush()
        for prod, qty, variant in items_spec:
            db.session.add(OrderItem(
                order_id=order.id, product_id=prod.id,
                product_name=prod.name, product_image=prod.image,
                variant=variant, quantity=qty, price=prod.price))
        return order

    # Alice's orders
    p_sony = _get_product('Sony WH-1000XM5')
    p_echo = _get_product('Echo Dot')
    p_atomic = _get_product('Atomic Habits')
    p_irobot = _get_product('iRobot Roomba')
    p_instant = _get_product('Instant Pot')
    p_adidas = _get_product('Cloudfoam Pure Running')

    if p_sony:
        _make_order(alice, 'processing', [(p_sony, 1, 'Color: Black')], days_ago=1)
    if p_echo:
        _make_order(alice, 'shipped', [(p_echo, 1, 'Color: Charcoal')], days_ago=5)
    if p_atomic and p_adidas:
        _make_order(alice, 'delivered', [(p_atomic, 1, 'Format: Paperback'), (p_adidas, 1, 'Size: 8, Color: White/Black')], days_ago=10)
    if p_irobot:
        _make_order(alice, 'delivered', [(p_irobot, 1, '')], days_ago=20)

    # Bob's orders
    p_kindle = _get_product('Kindle Paperwhite')
    p_yoga = _get_product('Amazon Basics Yoga Mat')
    if p_kindle:
        _make_order(bob, 'delivered', [(p_kindle, 1, '')], days_ago=8)
    if p_yoga:
        _make_order(bob, 'processing', [(p_yoga, 1, 'Color: Blue')], days_ago=1)

    # Carol's orders
    p_macbook = _get_product('MacBook')
    p_logi = _get_product('Logitech MX')
    if p_macbook and p_logi:
        _make_order(carol, 'delivered', [(p_macbook, 1, ''), (p_logi, 1, '')], days_ago=7)

    # David's orders
    p_samsung_tv = _get_product('Samsung')
    p_hp = _get_product('HP OMEN')
    if p_samsung_tv:
        _make_order(david, 'delivered', [(p_samsung_tv, 1, '')], days_ago=12)
    if p_hp:
        _make_order(david, 'processing', [(p_hp, 1, '')], days_ago=2)
    if p_instant:
        _make_order(david, 'cancelled', [(p_instant, 1, '')], days_ago=15)

    # Pre-seed cart items for Alice (for cart modification tasks)
    p_jeans = _get_product("501 Original")
    p_shoe = _get_product('Cloudfoam Pure Running')
    if p_jeans:
        db.session.add(CartItem(user_id=alice.id, product_id=p_jeans.id, quantity=1, variant='Size: 32x30, Color: Medium Wash'))
    if p_shoe:
        db.session.add(CartItem(user_id=alice.id, product_id=p_shoe.id, quantity=1, variant='Size: 8, Color: White/Black'))
    if p_echo:
        db.session.add(CartItem(user_id=alice.id, product_id=p_echo.id, quantity=1, variant='Color: Charcoal'))

    db.session.commit()


if __name__ == '__main__':
    with app.app_context():
        db.create_all()
        if Product.query.count() == 0:
            seed_database()
        seed_benchmark_users()
    port = int(os.environ.get('PORT', 28841))
    app.run(host='0.0.0.0', port=port, debug=False)
