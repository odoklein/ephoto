<?php
/**
 * Plugin Name:       Certif ID – Contrôle photos ANTS & Signature
 * Description:       Contrôle de conformité des photos d'identité (norme ANTS) et des signatures depuis WooCommerce, avec recadrage et transmission Make / ePhoto.
 * Version:           2.1.1
 * Requires at least: 5.8
 * Requires PHP:      7.4
 * Author:            Suzali
 * License:           GPL-2.0-or-later
 * Text Domain:       certif-ephoto-control
 * WC requires at least: 5.0
 * WC tested up to: 9.0
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

define( 'CERTIF_EPHOTO_VERSION', '2.1.1' );
define( 'CERTIF_EPHOTO_FILE', __FILE__ );
define( 'CERTIF_EPHOTO_DIR', plugin_dir_path( __FILE__ ) );
define( 'CERTIF_EPHOTO_URL', plugin_dir_url( __FILE__ ) );

// Declare HPOS compatibility.
add_action(
	'before_woocommerce_init',
	function () {
		if ( class_exists( '\Automattic\WooCommerce\Utilities\FeaturesUtil' ) ) {
			\Automattic\WooCommerce\Utilities\FeaturesUtil::declare_compatibility( 'custom_order_tables', __FILE__, true );
		}
	}
);

require_once CERTIF_EPHOTO_DIR . 'includes/class-certif-ephoto-order-reader.php';
require_once CERTIF_EPHOTO_DIR . 'includes/class-certif-ephoto-client.php';
require_once CERTIF_EPHOTO_DIR . 'includes/class-certif-ephoto-settings.php';
require_once CERTIF_EPHOTO_DIR . 'includes/class-certif-ephoto-hooks.php';
require_once CERTIF_EPHOTO_DIR . 'includes/class-certif-ephoto-admin.php';
require_once CERTIF_EPHOTO_DIR . 'includes/class-certif-ephoto-privacy.php';

// Settings shortcut on the plugins screen.
add_filter(
	'plugin_action_links_' . plugin_basename( __FILE__ ),
	function ( $links ) {
		$settings_link = '<a href="' . esc_url( admin_url( 'admin.php?page=' . Certif_Ephoto_Settings::PAGE ) ) . '">'
			. esc_html__( 'Réglages', 'certif-ephoto-control' ) . '</a>';
		array_unshift( $links, $settings_link );
		return $links;
	}
);

// WooCommerce dependency notice.
add_action(
	'admin_notices',
	function () {
		if ( ! class_exists( 'WooCommerce' ) && current_user_can( 'activate_plugins' ) ) {
			echo '<div class="notice notice-warning is-dismissible"><p><strong>'
				. esc_html__( 'Certif ID – Contrôle photos :', 'certif-ephoto-control' )
				. '</strong> '
				. esc_html__( 'Ce plugin nécessite WooCommerce pour lire les commandes et les photos d’identité.', 'certif-ephoto-control' )
				. '</p></div>';
		}
	}
);

/**
 * Initialize all components.
 */
function certif_ephoto_init() {
	Certif_Ephoto_Settings::init();
	Certif_Ephoto_Hooks::init();
	Certif_Ephoto_Admin::init();
	Certif_Ephoto_Privacy::init();
}
add_action( 'plugins_loaded', 'certif_ephoto_init' );
