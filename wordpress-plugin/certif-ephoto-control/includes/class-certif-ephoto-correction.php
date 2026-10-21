<?php
/**
 * Customer correction after a refusal.
 *
 * The refusal email used to point to the generic order wizard: the customer placed a
 * brand-new order (new checkout, new WooCommerce emails) that nothing tied back to the
 * refused one, which stayed « Refusé » in the review screen.
 *
 * Now the email carries a personal link (order id + HMAC token). It opens a short
 * upload form for THAT order; the new files are attached to it and re-analysed, so the
 * dossier comes back to « À traiter » by itself. The order waits in a dedicated status,
 * « Correction demandée », to which WooCommerce attaches no email.
 */

if ( ! defined( 'ABSPATH' ) ) {
	exit;
}

class Certif_Ephoto_Correction {

	const STATUS        = 'ephoto-correction'; // "wc-ephoto-correction": 20 chars, the post_status limit.
	const SHORTCODE     = 'certif_ephoto_correction';
	const QUERY_ORDER   = 'certif_correction';
	const QUERY_TOKEN   = 'k';
	const QUERY_DONE    = 'certif_done';
	const POST_ACTION   = 'certif_ephoto_correction';
	const INGEST_ACTION = 'certif_ephoto_ingest_correction';
	const MAX_ATTEMPTS  = 6; // Per order and per hour.

	public static function init() {
		add_action( 'init', array( __CLASS__, 'register_status' ) );
		add_filter( 'wc_order_statuses', array( __CLASS__, 'add_status' ) );
		add_filter( 'woocommerce_order_is_paid_statuses', array( __CLASS__, 'add_paid_status' ) );
		add_filter( 'woocommerce_reports_order_statuses', array( __CLASS__, 'add_paid_status' ) );

		add_shortcode( self::SHORTCODE, array( __CLASS__, 'shortcode' ) );
		add_filter( 'the_content', array( __CLASS__, 'filter_content' ), 20 );
		add_action( 'template_redirect', array( __CLASS__, 'prepare_page' ) );
		add_action( 'wp_enqueue_scripts', array( __CLASS__, 'register_assets' ) );

		add_action( 'admin_post_nopriv_' . self::POST_ACTION, array( __CLASS__, 'handle_submit' ) );
		add_action( 'admin_post_' . self::POST_ACTION, array( __CLASS__, 'handle_submit' ) );
		add_action( self::INGEST_ACTION, array( __CLASS__, 'run_ingest' ), 10, 1 );
	}

	/* ------------------------------------------------------------------
	 * Order status « Correction demandée »
	 * ------------------------------------------------------------------ */

	public static function register_status() {
		register_post_status(
			'wc-' . self::STATUS,
			array(
				'label'                     => _x( 'Correction demandée', 'Order status', 'certif-ephoto-control' ),
				'public'                    => false,
				'exclude_from_search'       => false,
				'show_in_admin_all_list'    => true,
				'show_in_admin_status_list' => true,
				/* translators: %s: number of orders */
				'label_count'               => _n_noop( 'Correction demandée <span class="count">(%s)</span>', 'Corrections demandées <span class="count">(%s)</span>', 'certif-ephoto-control' ),
			)
		);
	}

	public static function add_status( $statuses ) {
		$result = array();
		foreach ( $statuses as $key => $label ) {
			$result[ $key ] = $label;
			if ( 'wc-on-hold' === $key ) {
				$result[ 'wc-' . self::STATUS ] = _x( 'Correction demandée', 'Order status', 'certif-ephoto-control' );
			}
		}
		if ( ! isset( $result[ 'wc-' . self::STATUS ] ) ) {
			$result[ 'wc-' . self::STATUS ] = _x( 'Correction demandée', 'Order status', 'certif-ephoto-control' );
		}
		return $result;
	}

	/**
	 * The customer has paid: the order still counts as paid while it waits.
	 */
	public static function add_paid_status( $statuses ) {
		if ( is_array( $statuses ) && ! in_array( self::STATUS, $statuses, true ) ) {
			$statuses[] = self::STATUS;
		}
		return $statuses;
	}

	/* ------------------------------------------------------------------
	 * Personal link
	 * ------------------------------------------------------------------ */

	public static function page_id() {
		$page_id = absint( get_option( 'certif_ephoto_correction_page', 0 ) );
		return ( $page_id && 'publish' === get_post_status( $page_id ) ) ? $page_id : 0;
	}

	public static function enabled() {
		return 0 !== self::page_id();
	}

	/**
	 * How many corrections the customer has already sent for this order.
	 */
	public static function round_of( $order ) {
		return absint( $order->get_meta( '_certif_ephoto_correction_round' ) );
	}

	/**
	 * Token for the current round: a link stops working once it has been used.
	 */
	private static function token( $order, $round = null ) {
		$round = null === $round ? self::round_of( $order ) : absint( $round );
		$data  = 'correction|' . $order->get_id() . '|' . $round . '|' . $order->get_order_key();
		return substr( hash_hmac( 'sha256', $data, wp_salt( 'auth' ) ), 0, 32 );
	}

	/**
	 * Personal correction link of the order ('' when no correction page is set).
	 */
	public static function link( $order, $extra = array() ) {
		$page_id = self::page_id();
		if ( ! $order instanceof WC_Order || ! $page_id ) {
			return '';
		}
		$permalink = get_permalink( $page_id );
		if ( ! $permalink ) {
			return '';
		}
		return add_query_arg(
			array_merge(
				array(
					self::QUERY_ORDER => $order->get_id(),
					self::QUERY_TOKEN => self::token( $order ),
				),
				$extra
			),
			$permalink
		);
	}

	/**
	 * Order designated by a link, or null when the link is not valid.
	 */
	private static function order_from_link( $raw_order, $raw_token ) {
		$raw_order = is_scalar( $raw_order ) ? (string) $raw_order : '';
		$raw_token = is_scalar( $raw_token ) ? strtolower( (string) $raw_token ) : '';
		if ( ! preg_match( '/^\d{1,20}$/', $raw_order ) || ! preg_match( '/^[a-f0-9]{32}$/', $raw_token ) || ! function_exists( 'wc_get_order' ) ) {
			return null;
		}
		$order = wc_get_order( absint( $raw_order ) );
		if ( ! $order instanceof WC_Order || ! hash_equals( self::token( $order ), $raw_token ) ) {
			return null;
		}
		return $order;
	}

	/**
	 * Is the order waiting for the customer's files?
	 */
	public static function awaiting_customer( $order ) {
		return 'rejected' === (string) $order->get_meta( '_certif_ephoto_status' );
	}

	/**
	 * Has the customer sent files that are not analysed yet (dossier still refused)?
	 */
	public static function has_unprocessed_correction( $order ) {
		return self::round_of( $order ) > absint( $order->get_meta( '_certif_ephoto_correction_ingested_round' ) );
	}

	/**
	 * Called when a new dossier is created for the order: its corrections are analysed.
	 */
	public static function mark_ingested( $order ) {
		if ( self::round_of( $order ) > 0 ) {
			$order->update_meta_data( '_certif_ephoto_correction_ingested_round', self::round_of( $order ) );
		}
	}

	/**
	 * Remember, at refusal, what the customer must fix and the status to restore later.
	 *
	 * @param WC_Order   $order
	 * @param array|null $submission Body of GET /api/v1/submissions/{id}, if known.
	 */
	public static function remember_refusal( $order, $submission = null ) {
		if ( ! $order instanceof WC_Order ) {
			return;
		}
		if ( self::STATUS !== $order->get_status() ) {
			$order->update_meta_data( '_certif_ephoto_status_before_reject', $order->get_status() );
		}
		if ( is_array( $submission ) ) {
			$order->update_meta_data( '_certif_ephoto_failures', wp_json_encode( self::failures_of( $submission ) ) );
		}
	}

	/**
	 * Failed checks of a submission: array( 'photo' => [labels], 'signature' => [labels] ).
	 */
	public static function failures_of( $submission ) {
		$result = array(
			'photo'     => array(),
			'signature' => array(),
		);
		foreach ( array_keys( $result ) as $part ) {
			$report = isset( $submission[ $part ] ) && is_array( $submission[ $part ] ) ? $submission[ $part ] : array();
			$checks = isset( $report['checks'] ) && is_array( $report['checks'] ) ? $report['checks'] : array();
			foreach ( $checks as $check ) {
				if ( is_array( $check ) && isset( $check['status'], $check['label'] ) && 'fail' === $check['status'] && is_scalar( $check['label'] ) ) {
					$result[ $part ][] = sanitize_text_field( (string) $check['label'] );
				}
			}
		}
		return $result;
	}

	/**
	 * Stored failures of the last refusal (same shape as failures_of()).
	 */
	public static function stored_failures( $order ) {
		$decoded = json_decode( (string) $order->get_meta( '_certif_ephoto_failures' ), true );
		$result  = array(
			'photo'     => array(),
			'signature' => array(),
		);
		foreach ( array_keys( $result ) as $part ) {
			if ( is_array( $decoded ) && isset( $decoded[ $part ] ) && is_array( $decoded[ $part ] ) ) {
				$result[ $part ] = array_values( array_filter( array_map( 'strval', $decoded[ $part ] ) ) );
			}
		}
		return $result;
	}

	/* ------------------------------------------------------------------
	 * Front-end page
	 * ------------------------------------------------------------------ */

	private static function requested() {
		return isset( $_GET[ self::QUERY_ORDER ] ); // phpcs:ignore WordPress.Security.NonceVerification.Recommended
	}

	public static function register_assets() {
		wp_register_style( 'certif-ephoto-correction', plugins_url( 'assets/css/correction.css', CERTIF_EPHOTO_FILE ), array(), CERTIF_EPHOTO_VERSION );
		wp_register_script( 'certif-ephoto-correction', plugins_url( 'assets/js/correction.js', CERTIF_EPHOTO_FILE ), array(), CERTIF_EPHOTO_VERSION, true );
	}

	/**
	 * Personal pages are never cached nor indexed: they carry a token.
	 */
	public static function prepare_page() {
		if ( ! self::requested() ) {
			return;
		}
		nocache_headers();
		header( 'Referrer-Policy: no-referrer' );
		add_filter( 'wp_robots', 'wp_robots_no_robots' );
		self::register_assets();
		wp_enqueue_style( 'certif-ephoto-correction' );
		wp_enqueue_script( 'certif-ephoto-correction' );
	}

	/**
	 * On the configured page, a personal link replaces the page content with the form.
	 */
	public static function filter_content( $content ) {
		if ( ! self::requested() || ! self::page_id() || ! in_the_loop() || ! is_main_query() || ! is_page( self::page_id() ) ) {
			return $content;
		}
		if ( has_shortcode( (string) $content, self::SHORTCODE ) ) {
			return $content;
		}
		return self::render();
	}

	public static function shortcode() {
		return self::render();
	}

	/**
	 * The form, the confirmation or a clear explanation of why the link is not usable.
	 */
	public static function render() {
		self::register_assets();
		wp_enqueue_style( 'certif-ephoto-correction' );
		wp_enqueue_script( 'certif-ephoto-correction' );

		// phpcs:disable WordPress.Security.NonceVerification.Recommended
		$order = self::order_from_link(
			isset( $_GET[ self::QUERY_ORDER ] ) ? wp_unslash( $_GET[ self::QUERY_ORDER ] ) : '',
			isset( $_GET[ self::QUERY_TOKEN ] ) ? wp_unslash( $_GET[ self::QUERY_TOKEN ] ) : ''
		);
		$done  = isset( $_GET[ self::QUERY_DONE ] );
		$error = isset( $_GET['certif_error'] ) && is_string( $_GET['certif_error'] ) ? sanitize_key( wp_unslash( $_GET['certif_error'] ) ) : '';
		// phpcs:enable WordPress.Security.NonceVerification.Recommended

		ob_start();
		echo '<div class="certif-correction" id="certif-correction">';

		if ( ! $order && ! self::requested() ) {
			self::render_message(
				'neutral',
				__( 'Corriger votre commande', 'certif-ephoto-control' ),
				__( 'Ouvrez le lien personnel reçu dans notre e-mail : il vous amène directement au formulaire de votre commande.', 'certif-ephoto-control' )
			);
			echo '</div>';
			return ob_get_clean();
		}

		if ( ! $order ) {
			self::render_message(
				'neutral',
				__( 'Ce lien n’est plus valide', 'certif-ephoto-control' ),
				__( 'Il a peut-être déjà servi à envoyer vos fichiers. Utilisez le lien du dernier e-mail reçu, ou répondez à cet e-mail : nous vous aidons.', 'certif-ephoto-control' )
			);
			echo '</div>';
			return ob_get_clean();
		}

		if ( $done || ! self::awaiting_customer( $order ) ) {
			self::render_message(
				'success',
				__( 'Merci, vos fichiers sont bien arrivés', 'certif-ephoto-control' ),
				sprintf(
					/* translators: %s: order number */
					__( 'Nous vérifions à nouveau votre commande n° %s. Vous recevrez un e-mail dès que c’est fait : vous n’avez rien d’autre à faire.', 'certif-ephoto-control' ),
					$order->get_order_number()
				)
			);
			echo '</div>';
			return ob_get_clean();
		}

		self::render_form( $order, $error );
		echo '</div>';
		return ob_get_clean();
	}

	private static function render_message( $tone, $title, $text ) {
		?>
		<div class="certif-correction__card certif-correction__card--<?php echo esc_attr( $tone ); ?>" role="status">
			<span class="certif-correction__icon" aria-hidden="true"><?php echo 'success' === $tone ? '✓' : 'i'; ?></span>
			<h2 class="certif-correction__title"><?php echo esc_html( $title ); ?></h2>
			<p class="certif-correction__lead"><?php echo esc_html( $text ); ?></p>
		</div>
		<?php
	}

	private static function error_text( $code ) {
		$messages = array(
			'nofile'   => __( 'Ajoutez au moins une nouvelle photo ou une nouvelle signature.', 'certif-ephoto-control' ),
			'photo'    => __( 'La photo n’a pas pu être lue. Envoyez une image JPEG, PNG ou WebP de moins de 15 Mo.', 'certif-ephoto-control' ),
			'sign'     => __( 'La signature n’a pas pu être lue. Signez à nouveau dans le cadre ou envoyez une image JPEG, PNG ou WebP.', 'certif-ephoto-control' ),
			'save'     => __( 'L’enregistrement a échoué de notre côté. Réessayez dans un instant.', 'certif-ephoto-control' ),
			'too_many' => __( 'Trop d’envois en peu de temps. Réessayez dans une heure, ou répondez à notre e-mail.', 'certif-ephoto-control' ),
		);
		return isset( $messages[ $code ] ) ? $messages[ $code ] : '';
	}

	private static function render_form( $order, $error ) {
		$failures   = self::stored_failures( $order );
		$reason     = (string) $order->get_meta( '_certif_ephoto_reason' );
		$fix_photo  = ! empty( $failures['photo'] );
		$fix_sign   = ! empty( $failures['signature'] );
		$unknown    = ! $fix_photo && ! $fix_sign;
		$first_name = trim( (string) $order->get_billing_first_name() );
		$max_mb     = 15;
		?>
		<div class="certif-correction__card">
			<p class="certif-correction__eyebrow">
				<?php
				/* translators: %s: order number */
				echo esc_html( sprintf( __( 'Commande n° %s', 'certif-ephoto-control' ), $order->get_order_number() ) );
				?>
			</p>
			<h2 class="certif-correction__title">
				<?php
				echo esc_html(
					'' !== $first_name
						/* translators: %s: customer first name */
						? sprintf( __( '%s, il reste une petite correction', 'certif-ephoto-control' ), $first_name )
						: __( 'Il reste une petite correction', 'certif-ephoto-control' )
				);
				?>
			</h2>
			<p class="certif-correction__lead"><?php esc_html_e( 'Pas besoin de repasser commande ni de payer à nouveau : envoyez seulement ce qui est demandé ci-dessous, nous reprenons votre dossier là où il en était.', 'certif-ephoto-control' ); ?></p>

			<?php if ( '' !== $reason || ! $unknown ) : ?>
				<div class="certif-correction__reason">
					<h3><?php esc_html_e( 'Ce que notre contrôleur a relevé', 'certif-ephoto-control' ); ?></h3>
					<?php if ( '' !== $reason ) : ?>
						<p>« <?php echo esc_html( $reason ); ?> »</p>
					<?php endif; ?>
					<?php if ( ! $unknown ) : ?>
						<ul>
							<?php foreach ( $failures['photo'] as $label ) : ?>
								<li><strong><?php esc_html_e( 'Photo', 'certif-ephoto-control' ); ?></strong> · <?php echo esc_html( $label ); ?></li>
							<?php endforeach; ?>
							<?php foreach ( $failures['signature'] as $label ) : ?>
								<li><strong><?php esc_html_e( 'Signature', 'certif-ephoto-control' ); ?></strong> · <?php echo esc_html( $label ); ?></li>
							<?php endforeach; ?>
						</ul>
					<?php endif; ?>
				</div>
			<?php endif; ?>

			<?php if ( '' !== $error && '' !== self::error_text( $error ) ) : ?>
				<div class="certif-correction__alert" role="alert"><?php echo esc_html( self::error_text( $error ) ); ?></div>
			<?php endif; ?>

			<?php
			// Order and token also travel in the URL: when an upload exceeds post_max_size,
			// PHP drops the whole body and the handler must still know where to send back.
			$action_url = add_query_arg(
				array(
					'action' => self::POST_ACTION,
					'order'  => $order->get_id(),
					'k'      => self::token( $order ),
				),
				admin_url( 'admin-post.php' )
			);
			?>
			<form class="certif-correction__form" method="post" enctype="multipart/form-data" action="<?php echo esc_url( $action_url ); ?>" data-certif-correction data-max-bytes="<?php echo esc_attr( $max_mb * MB_IN_BYTES ); ?>" data-too-large="<?php echo esc_attr( self::error_text( 'photo' ) ); ?>">
				<input type="hidden" name="action" value="<?php echo esc_attr( self::POST_ACTION ); ?>">
				<input type="hidden" name="order" value="<?php echo esc_attr( $order->get_id() ); ?>">
				<input type="hidden" name="k" value="<?php echo esc_attr( self::token( $order ) ); ?>">

				<fieldset class="certif-correction__part<?php echo ( $fix_photo || $unknown ) ? ' is-requested' : ''; ?>">
					<legend>
						<span class="certif-correction__step">1</span>
						<?php esc_html_e( 'Nouvelle photo d’identité', 'certif-ephoto-control' ); ?>
						<span class="certif-correction__tag"><?php echo $fix_photo ? esc_html__( 'À corriger', 'certif-ephoto-control' ) : esc_html__( 'Facultatif', 'certif-ephoto-control' ); ?></span>
					</legend>
					<?php if ( ! $fix_photo && ! $unknown ) : ?>
						<p class="certif-correction__hint"><?php esc_html_e( 'Votre photo est conforme : laissez vide pour la garder.', 'certif-ephoto-control' ); ?></p>
					<?php else : ?>
						<p class="certif-correction__hint"><?php esc_html_e( 'De face, regard vers l’objectif, fond clair et uni, sans lunettes ni ombre sur le visage.', 'certif-ephoto-control' ); ?></p>
					<?php endif; ?>
					<label class="certif-correction__drop">
						<input type="file" name="photo" accept="image/jpeg,image/png,image/webp" data-preview="photo">
						<img alt="" hidden data-preview-img="photo">
						<span class="certif-correction__drop-text" data-preview-text="photo"><?php esc_html_e( 'Prendre ou choisir une photo', 'certif-ephoto-control' ); ?></span>
						<small><?php echo esc_html( sprintf( __( 'JPEG, PNG ou WebP · %d Mo maximum', 'certif-ephoto-control' ), $max_mb ) ); ?></small>
					</label>
				</fieldset>

				<fieldset class="certif-correction__part<?php echo ( $fix_sign || $unknown ) ? ' is-requested' : ''; ?>">
					<legend>
						<span class="certif-correction__step">2</span>
						<?php esc_html_e( 'Nouvelle signature', 'certif-ephoto-control' ); ?>
						<span class="certif-correction__tag"><?php echo $fix_sign ? esc_html__( 'À corriger', 'certif-ephoto-control' ) : esc_html__( 'Facultatif', 'certif-ephoto-control' ); ?></span>
					</legend>
					<?php if ( ! $fix_sign && ! $unknown ) : ?>
						<p class="certif-correction__hint"><?php esc_html_e( 'Votre signature est conforme : laissez vide pour la garder.', 'certif-ephoto-control' ); ?></p>
					<?php else : ?>
						<p class="certif-correction__hint"><?php esc_html_e( 'Signez dans le cadre avec le doigt ou la souris, comme sur vos papiers officiels. Vous pouvez aussi envoyer la photo d’une signature à l’encre noire sur papier blanc.', 'certif-ephoto-control' ); ?></p>
					<?php endif; ?>
					<div class="certif-correction__pad">
						<canvas width="1042" height="268" aria-label="<?php esc_attr_e( 'Zone de signature', 'certif-ephoto-control' ); ?>" data-signature-pad></canvas>
						<span class="certif-correction__pad-placeholder" data-signature-placeholder><?php esc_html_e( 'Signez ici', 'certif-ephoto-control' ); ?></span>
						<button type="button" class="certif-correction__link" data-signature-clear hidden><?php esc_html_e( 'Effacer', 'certif-ephoto-control' ); ?></button>
					</div>
					<input type="hidden" name="signature_drawn" value="" data-signature-value>
					<details class="certif-correction__alt">
						<summary><?php esc_html_e( 'Envoyer plutôt une image de ma signature', 'certif-ephoto-control' ); ?></summary>
						<input type="file" name="signature" accept="image/jpeg,image/png,image/webp">
					</details>
				</fieldset>

				<p class="certif-correction__client-error" role="alert" hidden data-form-error><?php echo esc_html( self::error_text( 'nofile' ) ); ?></p>
				<button type="submit" class="certif-correction__submit" data-submit>
					<span data-submit-label><?php esc_html_e( 'Envoyer ma correction', 'certif-ephoto-control' ); ?></span>
					<span hidden data-submit-busy><?php esc_html_e( 'Envoi en cours…', 'certif-ephoto-control' ); ?></span>
				</button>
				<p class="certif-correction__fineprint"><?php esc_html_e( 'Vos fichiers servent uniquement à préparer votre photo d’identité numérique et sont supprimés après traitement.', 'certif-ephoto-control' ); ?></p>
			</form>
		</div>
		<?php
	}

	/* ------------------------------------------------------------------
	 * Submission
	 * ------------------------------------------------------------------ */

	private static function back( $order, $args ) {
		$url = $order ? self::link( $order, $args ) : '';
		wp_safe_redirect( '' !== $url ? $url : home_url( '/' ) );
		exit;
	}

	public static function handle_submit() {
		// phpcs:disable WordPress.Security.NonceVerification.Missing -- authenticated by the per-order HMAC token.
		// phpcs:disable WordPress.Security.NonceVerification.Recommended
		$order = self::order_from_link(
			isset( $_REQUEST['order'] ) ? wp_unslash( $_REQUEST['order'] ) : '',
			isset( $_REQUEST['k'] ) ? wp_unslash( $_REQUEST['k'] ) : ''
		);
		// phpcs:enable WordPress.Security.NonceVerification.Recommended
		if ( ! $order ) {
			wp_safe_redirect( home_url( '/' ) );
			exit;
		}
		if ( empty( $_POST ) && ! empty( $_SERVER['CONTENT_LENGTH'] ) ) {
			self::back( $order, array( 'certif_error' => 'photo' ) ); // Body over post_max_size.
		}
		if ( ! self::awaiting_customer( $order ) ) {
			self::back( $order, array( self::QUERY_DONE => 1 ) );
		}

		$attempts_key = 'certif_ephoto_correction_' . $order->get_id();
		$attempts     = absint( get_transient( $attempts_key ) );
		if ( $attempts >= self::MAX_ATTEMPTS ) {
			self::back( $order, array( 'certif_error' => 'too_many' ) );
		}
		set_transient( $attempts_key, $attempts + 1, HOUR_IN_SECONDS );

		$photo     = self::uploaded_image( 'photo' );
		$signature = self::uploaded_image( 'signature' );
		if ( null === $signature ) {
			$signature = self::drawn_signature( isset( $_POST['signature_drawn'] ) && is_string( $_POST['signature_drawn'] ) ? wp_unslash( $_POST['signature_drawn'] ) : '' ); // phpcs:ignore WordPress.Security.ValidatedSanitizedInput.InputNotSanitized
		}
		// phpcs:enable WordPress.Security.NonceVerification.Missing

		if ( false === $photo ) {
			self::back( $order, array( 'certif_error' => 'photo' ) );
		}
		if ( false === $signature ) {
			self::back( $order, array( 'certif_error' => 'sign' ) );
		}
		if ( null === $photo && null === $signature ) {
			self::back( $order, array( 'certif_error' => 'nofile' ) );
		}

		$saved = array();
		foreach ( array( 'photo' => $photo, 'signature' => $signature ) as $part => $image ) {
			if ( null === $image ) {
				continue;
			}
			$url = self::store( $order, $part, $image );
			if ( '' === $url ) {
				self::back( $order, array( 'certif_error' => 'save' ) );
			}
			$saved[ $part ] = $url;
		}

		self::accept_correction( $order, $saved );
		$done = self::link( $order, array( self::QUERY_DONE => 1 ) );
		wp_safe_redirect( '' !== $done ? $done : home_url( '/' ) );
		exit;
	}

	/**
	 * A validated image from $_FILES: array(bytes, ext), null when absent, false when unusable.
	 */
	private static function uploaded_image( $field ) {
		// phpcs:disable WordPress.Security.NonceVerification.Missing,WordPress.Security.ValidatedSanitizedInput
		if ( empty( $_FILES[ $field ] ) || ! is_array( $_FILES[ $field ] ) || ! isset( $_FILES[ $field ]['error'] ) || is_array( $_FILES[ $field ]['error'] ) ) {
			return null;
		}
		$file = $_FILES[ $field ];
		// phpcs:enable WordPress.Security.NonceVerification.Missing,WordPress.Security.ValidatedSanitizedInput
		if ( UPLOAD_ERR_NO_FILE === (int) $file['error'] ) {
			return null;
		}
		if ( UPLOAD_ERR_OK !== (int) $file['error'] || empty( $file['tmp_name'] ) || ! is_uploaded_file( $file['tmp_name'] ) ) {
			return false;
		}
		if ( (int) $file['size'] <= 0 || (int) $file['size'] > 15 * MB_IN_BYTES ) {
			return false;
		}
		$bytes = file_get_contents( $file['tmp_name'] ); // phpcs:ignore WordPress.WP.AlternativeFunctions.file_get_contents_file_get_contents
		return false === $bytes ? false : self::validated( $bytes );
	}

	/**
	 * Signature drawn on the canvas, sent as a PNG data URL.
	 */
	private static function drawn_signature( $data_url ) {
		$data_url = trim( (string) $data_url );
		if ( '' === $data_url ) {
			return null;
		}
		if ( strlen( $data_url ) > 4 * MB_IN_BYTES || 0 !== strpos( $data_url, 'data:image/png;base64,' ) ) {
			return false;
		}
		$bytes = base64_decode( substr( $data_url, strlen( 'data:image/png;base64,' ) ), true ); // phpcs:ignore WordPress.PHP.DiscouragedPHPFunctions.obfuscation_base64_decode
		return false === $bytes ? false : self::validated( $bytes );
	}

	/**
	 * Bytes that really are a JPEG/PNG/WebP image of sensible size.
	 */
	private static function validated( $bytes ) {
		$info = @getimagesizefromstring( $bytes ); // phpcs:ignore WordPress.PHP.NoSilencedErrors.Discouraged
		if ( ! is_array( $info ) || empty( $info[0] ) || empty( $info[1] ) || $info[0] * $info[1] > 60000000 ) {
			return false;
		}
		$types = array(
			IMAGETYPE_JPEG => 'jpg',
			IMAGETYPE_PNG  => 'png',
		);
		if ( defined( 'IMAGETYPE_WEBP' ) ) {
			$types[ IMAGETYPE_WEBP ] = 'webp';
		}
		if ( ! isset( $info[2], $types[ $info[2] ] ) ) {
			return false;
		}
		return array(
			'bytes' => $bytes,
			'ext'   => $types[ $info[2] ],
		);
	}

	/**
	 * Write the file under uploads with an unguessable name; returns its URL or ''.
	 */
	private static function store( $order, $part, $image ) {
		$name   = sprintf( 'certif-correction-%d-%s-%s.%s', $order->get_id(), $part, wp_generate_password( 20, false, false ), $image['ext'] );
		$result = wp_upload_bits( $name, null, $image['bytes'] );
		if ( ! empty( $result['error'] ) || empty( $result['url'] ) ) {
			return '';
		}
		return Certif_Ephoto_Order_Reader::is_allowed_upload_url( $result['url'] ) ? (string) $result['url'] : '';
	}

	/**
	 * Attach the new files to the order and queue a new analysis.
	 */
	private static function accept_correction( $order, $saved ) {
		foreach ( $saved as $part => $url ) {
			$order->update_meta_data( '_certif_ephoto_correction_' . $part . '_url', $url );
		}
		$order->update_meta_data( '_certif_ephoto_correction_round', self::round_of( $order ) + 1 );
		$order->update_meta_data( '_certif_ephoto_corrected_at', current_time( 'mysql' ) ); // Display only.

		// Back to the status the order had before the refusal. From « Correction
		// demandée » WooCommerce sends no email on this change.
		$previous = (string) $order->get_meta( '_certif_ephoto_status_before_reject' );
		if ( self::STATUS === $order->get_status() && '' !== $previous && self::STATUS !== $previous
			&& array_key_exists( 'wc-' . $previous, wc_get_order_statuses() ) ) {
			$order->set_status( $previous, __( 'Correction reçue du client.', 'certif-ephoto-control' ) );
		}
		$order->save();

		$parts = array();
		if ( isset( $saved['photo'] ) ) {
			$parts[] = __( 'photo', 'certif-ephoto-control' );
		}
		if ( isset( $saved['signature'] ) ) {
			$parts[] = __( 'signature', 'certif-ephoto-control' );
		}
		$order->add_order_note(
			sprintf(
				/* translators: %s: "photo", "signature" or both */
				__( '[Certif ID] Correction envoyée par le client via son lien personnel (%s). Nouvelle analyse lancée.', 'certif-ephoto-control' ),
				implode( ' + ', $parts )
			)
		);

		self::schedule_ingest( $order->get_id() );
	}

	public static function schedule_ingest( $order_id ) {
		$args = array( absint( $order_id ) );
		if ( function_exists( 'as_enqueue_async_action' ) ) {
			as_enqueue_async_action( self::INGEST_ACTION, $args, Certif_Ephoto_Hooks::AS_GROUP );
			return;
		}
		self::run_ingest( $order_id );
	}

	/**
	 * Re-analysis of a corrected dossier (runs whatever the « Envoi automatique » setting:
	 * the correction form belongs to this plugin).
	 */
	public static function run_ingest( $order_id ) {
		$result = Certif_Ephoto_Client::ingest_order( absint( $order_id ), true );
		if ( ! is_wp_error( $result ) ) {
			return;
		}
		if ( 'ingest_locked' === $result->get_error_code() ) {
			if ( function_exists( 'as_schedule_single_action' ) ) {
				as_schedule_single_action( time() + MINUTE_IN_SECONDS, self::INGEST_ACTION, array( absint( $order_id ) ), Certif_Ephoto_Hooks::AS_GROUP );
			}
			return;
		}
		$order = wc_get_order( absint( $order_id ) );
		if ( $order instanceof WC_Order ) {
			$order->add_order_note(
				sprintf(
					/* translators: %s: error message */
					__( '[Certif ID] La correction du client n’a pas pu être envoyée au service : %s. Utilisez « Relancer l’analyse » dans Contrôle photos.', 'certif-ephoto-control' ),
					$result->get_error_message()
				)
			);
		}
	}
}
