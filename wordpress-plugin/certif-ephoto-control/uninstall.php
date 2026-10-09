<?php
/**
 * Runs only when the plugin is deleted from the WordPress admin.
 * Removes the plugin's options, lock transients and `_certif_ephoto_*` order meta,
 * on every site of a multisite network.
 */

if ( ! defined( 'WP_UNINSTALL_PLUGIN' ) ) {
	exit;
}

if ( ! function_exists( 'certif_ephoto_uninstall_site' ) ) {
	/**
	 * Clean one site.
	 *
	 * @param bool $use_wc_api Use the WooCommerce API (only for the site WooCommerce was loaded for).
	 */
	function certif_ephoto_uninstall_site( $use_wc_api ) {
		global $wpdb;

		$options = array(
			'certif_ephoto_service_url',
			'certif_ephoto_api_key',
			'certif_ephoto_review_key',
			'certif_ephoto_make_webhook_url',
			'certif_ephoto_auto_ingest',
			'certif_ephoto_accepted_status',
			'certif_ephoto_rejected_status',
			'certif_ephoto_photo_meta_key',
			'certif_ephoto_sign_meta_key',
			'certif_ephoto_db_version',
		);
		foreach ( $options as $option ) {
			delete_option( $option );
		}

		// Ingest lock transients.
		$wpdb->query( // phpcs:ignore WordPress.DB.DirectDatabaseQuery
			$wpdb->prepare(
				"DELETE FROM {$wpdb->options} WHERE option_name LIKE %s OR option_name LIKE %s",
				$wpdb->esc_like( '_transient_certif_ephoto_' ) . '%',
				$wpdb->esc_like( '_transient_timeout_certif_ephoto_' ) . '%'
			)
		);

		$meta_like = $wpdb->esc_like( '_certif_ephoto_' ) . '%';

		// 1. Through WooCommerce when it is loaded (keeps caches and HPOS sync consistent).
		if ( $use_wc_api && function_exists( 'wc_get_orders' ) && class_exists( 'WC_Order' ) ) {
			$seen = array();
			for ( $batch = 0; $batch < 500; $batch++ ) {
				$orders = wc_get_orders(
					array(
						'type'       => 'shop_order',
						'limit'      => 100,
						'return'     => 'objects',
						'meta_query' => array( // phpcs:ignore WordPress.DB.SlowDBQuery.slow_db_query_meta_query
							array(
								'key'     => '_certif_ephoto_submission_id',
								'compare' => 'EXISTS',
							),
						),
					)
				);
				if ( ! is_array( $orders ) || empty( $orders ) ) {
					break;
				}
				$progress = false;
				foreach ( $orders as $order ) {
					if ( ! $order instanceof WC_Order ) {
						continue;
					}
					$id = $order->get_id();
					if ( isset( $seen[ $id ] ) ) {
						continue;
					}
					$seen[ $id ] = true;
					$progress    = true;
					foreach ( $order->get_meta_data() as $meta ) {
						if ( 0 === strpos( (string) $meta->key, '_certif_ephoto_' ) ) {
							$order->delete_meta_data( $meta->key );
						}
					}
					$order->save();
				}
				if ( ! $progress ) {
					break; // Same orders returned again: let the SQL sweep below finish the job.
				}
			}
		}

		// 2. SQL sweep (WooCommerce not loaded, trashed orders, leftovers).
		$wpdb->query( // phpcs:ignore WordPress.DB.DirectDatabaseQuery
			$wpdb->prepare( "DELETE FROM {$wpdb->postmeta} WHERE meta_key LIKE %s", $meta_like )
		);

		$hpos_meta = $wpdb->prefix . 'wc_orders_meta';
		$found     = $wpdb->get_var( $wpdb->prepare( 'SHOW TABLES LIKE %s', $wpdb->esc_like( $hpos_meta ) ) ); // phpcs:ignore WordPress.DB.DirectDatabaseQuery
		if ( $found === $hpos_meta ) {
			$wpdb->query( // phpcs:ignore WordPress.DB.DirectDatabaseQuery
				$wpdb->prepare( "DELETE FROM {$hpos_meta} WHERE meta_key LIKE %s", $meta_like ) // phpcs:ignore WordPress.DB.PreparedSQL.InterpolatedNotPrepared
			);
		}
	}
}

if ( is_multisite() && function_exists( 'get_sites' ) ) {
	$certif_ephoto_current = get_current_blog_id();
	$certif_ephoto_sites   = get_sites(
		array(
			'fields' => 'ids',
			'number' => 0,
		)
	);
	foreach ( $certif_ephoto_sites as $certif_ephoto_site_id ) {
		switch_to_blog( (int) $certif_ephoto_site_id );
		certif_ephoto_uninstall_site( (int) $certif_ephoto_site_id === (int) $certif_ephoto_current );
		restore_current_blog();
	}
} else {
	certif_ephoto_uninstall_site( true );
}
